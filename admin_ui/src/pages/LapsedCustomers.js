import React, { useState } from 'react';
import {
  Alert, Button, Card, Col, Descriptions, Input, Modal, Radio, Row, Segmented, Space, Statistic, Table, Tag,
  Typography, message,
} from 'antd';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import adminService from '../services/adminService';
import { useAuthStore } from '../stores/authStore';
import { DEFAULT_PAGE_SIZE, LAPSED_CUSTOMERS_SUMMARY_KEY } from '../utils/constants';
import { apiErrorCode, extractApiErrorMessage } from '../utils/apiError';
import { formatDate } from '../utils/dateUtils';
import { formatMoneyUZS } from '../utils/formatMoney';
import { describeReminderSetting, REMINDER_SETTING_STATE } from '../utils/reorderReminderSetting';

const { Title, Text } = Typography;

// Presentation only: a colour per published flag. A flag this map does not know still renders.
const FLAG_COLORS = new Map([
  ['no_response', 'gold'],
  ['no_channel', 'orange'],
  ['stopped', 'volcano'],
  ['blocked', 'red'],
  ['snoozed', 'blue'],
  ['replied', 'purple'],
  ['stuck_order', 'magenta'],
  ['long_lapsed', 'default'],
  ['outlet_dormant', 'cyan'],
]);
const flagColor = (flag) => FLAG_COLORS.get(flag) || 'default';
// The tabs whose rows can carry an escalation flag, so the flag chips mean something there: a
// follow-up, a paused row that had escalated before it was suspended (it keeps its flag), and a
// resolved one. An open (Being reminded) row has not escalated and has no flag.
const FLAGGED_TABS = ['follow_up', 'paused', 'resolved'];
// An event's `detail` is a published code of the vocabulary named by the event's kind; the other
// kinds' details (reason codes, "already_off", ...) are printed as published.
const EVENT_DETAIL_VOCABULARY = new Map([['escalated', 'flag'], ['resolved', 'outcome'], ['suspended', 'suspend']]);
// The outcome that opens the list on a row the customer asked not to be contacted (spec §9.1).
const DO_NOT_CONTACT = 'do_not_contact';
const NO_VALUE = '—';
// The one refusal this page explains itself, in the admin's language (a colleague resolved the same
// lapse first): the resolve request names it, so api.js does not toast it as well. Held to the
// backend's raise site by tests/unit/test_lapsed_customers_ui_contract.py.
const RESOLVE_HANDLED_CODES = ['REORDER_REMINDER_NOT_RESOLVABLE'];

// A published object keyed by a published code (`summary.counts`, `metrics.escalated_by_flag`).
const publishedValue = (published, code) => {
  // eslint-disable-next-line security/detect-object-injection
  return published && Object.prototype.hasOwnProperty.call(published, code) ? published[code] : undefined;
};

/**
 * The Lapsed customers worklist (reorder-reminders spec §9.1), for admins and managers.
 *
 * Every row field, the vocabularies, `can_resolve`, `can_turn_off_reminders`, `pending_close` and
 * `contact_restriction` are the backend's. The page only orders the outcome list (do_not_contact
 * first on a restricted row) and renders. Recording an outcome or turning reminders off refreshes
 * the rows, the summary and the nav badge, which share the `lapsedCustomers` query prefix. The
 * summary costs a dozen queries, so it is read once on load and shared by key; switching tabs,
 * chips or pages re-reads only the rows.
 */
const LapsedCustomers = () => {
  const { t } = useTranslation('common');
  const queryClient = useQueryClient();
  const { hasPermission } = useAuthStore();
  const canManage = hasPermission('can_manage_lapsed_customers');

  const [tab, setTab] = useState('follow_up');
  const [flag, setFlag] = useState(null);
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const [resolving, setResolving] = useState(null);
  const [outcome, setOutcome] = useState(null);
  const [note, setNote] = useState('');
  const [turningOff, setTurningOff] = useState(null);
  const [reason, setReason] = useState('');

  const summaryQuery = useQuery({
    queryKey: LAPSED_CUSTOMERS_SUMMARY_KEY,
    queryFn: () => adminService.getReorderReminderSummary(),
    enabled: canManage,
    staleTime: 30_000,
  });
  const summary = summaryQuery.data;

  const listQuery = useQuery({
    queryKey: ['lapsedCustomers', 'episodes', tab, flag, search, page],
    queryFn: () => adminService.listReorderReminderEpisodes({ tab, flag, search, page, perPage: DEFAULT_PAGE_SIZE }),
    enabled: canManage,
    placeholderData: keepPreviousData,
    // *Needs follow-up* reads every escalated row's facts, so a window refocus must not re-run it.
    staleTime: 30_000,
  });
  const listing = listQuery.data;

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['lapsedCustomers'] });

  const closeResolve = () => {
    setResolving(null);
    setOutcome(null);
    setNote('');
  };
  const closeTurnOff = () => {
    setTurningOff(null);
    setReason('');
  };

  // A refusal is toasted by api.js, except the one this page explains itself. The modals stay open
  // on a refusal so the admin can retry, unless the row itself changed under them.
  const resolveMutation = useMutation({
    mutationFn: ({ episodeId, chosenOutcome, text }) => adminService.resolveReorderReminderEpisode(episodeId, {
      outcome: chosenOutcome,
      note: text.trim(),
      handledErrorCodes: RESOLVE_HANDLED_CODES,
    }),
    onSuccess: () => {
      message.success(t('ui.lapsed.resolve_ok', 'Outcome saved'));
      refresh();
      closeResolve();
    },
    onError: (error) => {
      const alreadyHandled = RESOLVE_HANDLED_CODES.includes(apiErrorCode(error));
      if (alreadyHandled) {
        message.warning(t(
          'ui.lapsed.not_resolvable',
          'This lapse was already handled by someone else. The list has been refreshed.',
        ));
      }
      if (alreadyHandled || error?.response?.status === 404) {
        refresh();
        closeResolve();
      }
    },
  });
  const turnOffMutation = useMutation({
    mutationFn: ({ userId, text }) => adminService.updateUserNotificationSettings(userId, {
      reorder_reminders_enabled: false,
      reason: text,
    }),
    onSuccess: () => {
      message.success(t('ui.lapsed.turn_off_ok', 'Reminders turned off'));
      refresh();
      closeTurnOff();
    },
  });

  const confirmTurnOff = () => {
    const text = reason.trim();
    if (!text) {
      message.error(t('ui.lapsed.reason_required', 'Reason is required'));
      return;
    }
    turnOffMutation.mutate({ userId: turningOff.recipient.user_id, text });
  };

  const applySearch = (value) => {
    setSearch((value || '').trim());
    setPage(1);
  };
  const applyFlag = (value) => {
    setFlag(value);
    setPage(1);
  };
  const chooseTab = (value) => {
    setTab(value);
    setFlag(null);
    setPage(1);
  };

  const restrictionText = (restriction) => (restriction.kind === 'blocked'
    ? t('ui.lapsed.restriction_blocked', 'Customer blocked the bot on {{date}}.', { date: formatDate(restriction.since) })
    : t(
      'ui.lapsed.restriction_stopped',
      'Customer switched reorder reminders off on {{date}} via {{via}} — service follow-up only.',
      {
        date: formatDate(restriction.since),
        via: restriction.via ? t(`ui.reorder_reminders.via.${restriction.via}`, restriction.via) : NO_VALUE,
      },
    ));

  const outcomeOptions = (row) => {
    const outcomes = summary?.outcomes || [];
    // §9.1: on a restricted row (opted out or blocked), "do not contact" is offered first.
    const ordered = row?.contact_restriction
      ? [DO_NOT_CONTACT, ...outcomes.filter((value) => value !== DO_NOT_CONTACT)]
      : outcomes;
    return ordered.map((value) => ({ value, label: t(`ui.lapsed.outcome.${value}`, value) }));
  };

  // Spec §9.1: the shared place's label AND address, so staff can tell two places apart. Either one
  // alone still names it; neither falls back to the group id.
  const renderPlaceTitle = (place) => (place.label && place.address
    ? t('ui.lapsed.place_title_address', 'Shared place: {{label}} · {{address}}', { label: place.label, address: place.address })
    : t('ui.lapsed.place_title', 'Shared place: {{label}}', { label: place.label || place.address || `#${place.group_id}` }));

  const eventDetail = (event) => {
    const vocabulary = EVENT_DETAIL_VOCABULARY.get(event.kind);
    return vocabulary && event.detail ? t(`ui.lapsed.${vocabulary}.${event.detail}`, event.detail) : event.detail;
  };

  const renderCustomer = (row) => (
    <Space direction="vertical" size={2}>
      <Space size={4} wrap>
        <Text strong>{row.recipient?.name || `#${row.recipient?.user_id}`}</Text>
        {/* The language the reminder is written in, as the backend resolved it (one rule). */}
        {row.recipient?.reminder_language ? (
          <Tag title={t('ui.lapsed.reminder_language', 'Reminder language')}>
            {row.recipient.reminder_language.toUpperCase()}
          </Tag>
        ) : null}
        {row.is_dry_run ? <Tag color="orange">{t('ui.lapsed.dry_run_row', 'Simulated')}</Tag> : null}
      </Space>
      {row.phones.map((phone) => (
        <Space key={phone.user_id} size={4} wrap>
          <Text>{phone.phone || NO_VALUE}</Text>
          {phone.is_recipient ? <Tag color="blue">{t('ui.lapsed.recipient', 'reminded account')}</Tag> : null}
          {phone.blocked_bot ? <Tag color="red">{t('ui.lapsed.blocked_bot', 'blocked bot')}</Tag> : null}
        </Space>
      ))}
      {row.contact_restriction ? (
        <Alert type="warning" showIcon message={restrictionText(row.contact_restriction)} />
      ) : null}
      {row.places.map((place) => (
        <div key={place.group_id}>
          <Text>{renderPlaceTitle(place)}</Text>
          {place.members.map((member) => (
            <div key={member.user_id}>
              <Text type="secondary">
                {member.last_delivery_at
                  ? t('ui.lapsed.place_member', '{{name}} · {{phone}} · last delivery {{date}}', {
                    name: member.name || `#${member.user_id}`,
                    phone: member.phone || NO_VALUE,
                    date: formatDate(member.last_delivery_at),
                  })
                  : t('ui.lapsed.place_member_never', '{{name}} · {{phone}} · no delivery here', {
                    name: member.name || `#${member.user_id}`,
                    phone: member.phone || NO_VALUE,
                  })}
              </Text>
            </div>
          ))}
        </div>
      ))}
      {/* null = the escalation's outlet record could not be read; [] = no outlets to show. */}
      {row.outlets === null ? (
        <Text type="secondary">{t('ui.lapsed.outlets_unavailable', 'Outlet details are unavailable')}</Text>
      ) : null}
      {(row.outlets || []).map((outlet) => (
        <div key={outlet.outlet_id}>
          <Text type="secondary">
            {t('ui.lapsed.outlet', 'Outlet {{name}} · dormant since {{date}} · agent {{agent}}', {
              name: outlet.name,
              date: outlet.dormant_since ? formatDate(outlet.dormant_since) : NO_VALUE,
              agent: outlet.agent?.name || t('ui.lapsed.outlet_no_agent', 'none'),
            })}
          </Text>
        </div>
      ))}
    </Space>
  );

  const renderLapse = (row) => (
    <Space direction="vertical" size={0}>
      <Text>
        {t('ui.lapsed.last_delivery', 'Last delivery {{date}} · {{days}} days ago', {
          date: formatDate(row.last_delivery_at), days: row.days_since,
        })}
      </Text>
      <Text type="secondary">
        {row.gap_source === 'personal'
          ? t('ui.lapsed.rhythm_personal', 'Usual gap {{gap}} days · threshold {{threshold}}', {
            gap: row.usual_gap_days, threshold: row.threshold_days,
          })
          : t('ui.lapsed.rhythm_preset', 'Preset · threshold {{threshold}} days', { threshold: row.threshold_days })}
      </Text>
      <Text type="secondary">
        {t('ui.lapsed.days_overdue', '{{days}} days overdue (due {{date}})', {
          days: row.days_overdue, date: formatDate(row.due_on),
        })}
      </Text>
    </Space>
  );

  const renderReminders = (row) => (
    <Space direction="vertical" size={0}>
      <Text>
        {row.next_action_on
          ? t('ui.lapsed.step_next', 'Step {{step}} · next {{date}}', { step: row.step, date: formatDate(row.next_action_on) })
          : t('ui.lapsed.step', 'Step {{step}}', { step: row.step })}
      </Text>
      {row.history.map((event, index) => (
        // The history has no id of its own and may repeat a kind within one instant.
        // eslint-disable-next-line react/no-array-index-key
        <Text key={`${event.kind}-${event.at}-${index}`} type="secondary">
          {[
            formatDate(event.at),
            t(`ui.lapsed.event.${event.kind}`, event.kind),
            event.channel ? t(`ui.lapsed.channel.${event.channel}`, event.channel) : null,
            eventDetail(event),
          ].filter(Boolean).join(' · ')}
        </Text>
      ))}
      <Text type={row.setting && row.setting.state !== REMINDER_SETTING_STATE.ON ? 'danger' : undefined}>
        {describeReminderSetting(t, row.setting)}
      </Text>
    </Space>
  );

  const renderStatus = (row) => (
    <Space direction="vertical" size={2}>
      {row.escalation_flag ? (
        <Tag color={flagColor(row.escalation_flag)}>{t(`ui.lapsed.flag.${row.escalation_flag}`, row.escalation_flag)}</Tag>
      ) : null}
      {/* The next daily run closes this lapse: the customer has already reordered. */}
      {row.pending_close ? (
        <Tag color="green">{t('ui.lapsed.pending_close', 'Already reordered — closes at the next run')}</Tag>
      ) : null}
      {row.suspended_reason ? <Tag>{t(`ui.lapsed.suspend.${row.suspended_reason}`, row.suspended_reason)}</Tag> : null}
      {row.outcome ? (
        <Text>{t('ui.lapsed.outcome_label', 'Outcome: {{outcome}}', { outcome: t(`ui.lapsed.outcome.${row.outcome}`, row.outcome) })}</Text>
      ) : null}
      {row.outcome_note ? <Text type="secondary">{row.outcome_note}</Text> : null}
      {row.reply_excerpt ? (
        <Text italic>{t('ui.lapsed.reply', 'Customer replied: “{{excerpt}}”', { excerpt: row.reply_excerpt })}</Text>
      ) : null}
      {row.stuck_orders.map((order) => (
        <Text key={order.order_id} type="warning">
          {t('ui.lapsed.stuck_order', 'Order {{number}} · {{status}} · {{days}} days old', {
            number: order.order_number || `#${order.order_id}`,
            status: t(`ui.lapsed.order_status.${order.status}`, order.status),
            days: order.age_days,
          })}
          {order.awaiting_new_date ? ` · ${t('ui.lapsed.awaiting_new_date', 'awaiting a new date')}` : ''}
        </Text>
      ))}
    </Space>
  );

  const columns = [
    { title: t('ui.lapsed.col.customer', 'Customer'), key: 'customer', width: 340, render: (_, row) => renderCustomer(row) },
    { title: t('ui.lapsed.col.lapse', 'Lapse'), key: 'lapse', width: 240, render: (_, row) => renderLapse(row) },
    { title: t('ui.lapsed.col.reminders', 'Reminders'), key: 'reminders', width: 260, render: (_, row) => renderReminders(row) },
    { title: t('ui.lapsed.col.status', 'Reason'), key: 'status', width: 220, render: (_, row) => renderStatus(row) },
    {
      title: t('ui.lapsed.col.value', 'Value'),
      key: 'value',
      width: 200,
      render: (_, row) => t('ui.lapsed.value', '{{deliveries}} deliveries · {{bottles}} bottles held · debt {{debt}}', {
        deliveries: row.lifetime_deliveries, bottles: row.bottles_held, debt: formatMoneyUZS(row.open_debt),
      }),
    },
    {
      title: t('ui.lapsed.col.actions', 'Actions'),
      key: 'actions',
      width: 170,
      render: (_, row) => (
        <Space direction="vertical">
          {row.can_resolve ? (
            <Button type="primary" size="small" onClick={() => setResolving(row)}>
              {t('ui.lapsed.action.resolve', 'Record outcome')}
            </Button>
          ) : null}
          {row.can_turn_off_reminders ? (
            <Button danger size="small" onClick={() => setTurningOff(row)}>
              {t('ui.lapsed.action.turn_off', 'Turn off reminders')}
            </Button>
          ) : null}
        </Space>
      ),
    },
  ];

  const metrics = summary?.metrics_30d;
  const resolveOptions = outcomeOptions(resolving);
  const sentLabel = summary?.row_set_is_dry_run
    ? t('ui.lapsed.metric.reminders_simulated', 'Reminders simulated')
    : t('ui.lapsed.metric.reminders_sent', 'Reminders sent');
  // The counts the backend publishes are the tabs that carry a badge; the other tabs show none.
  const tabOptions = (summary?.tabs || [tab]).map((value) => ({
    value,
    label: publishedValue(summary?.counts, value) !== undefined
      ? `${t(`ui.lapsed.tab.${value}`, value)} (${publishedValue(summary.counts, value)})`
      : t(`ui.lapsed.tab.${value}`, value),
  }));

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Title level={3}>{t('ui.lapsed.title', 'Lapsed customers')}</Title>
        <Text type="secondary">
          {t('ui.lapsed.hint', 'Customers who stopped ordering against their own rhythm. Record an outcome for every row you work.')}
        </Text>
      </div>

      {summaryQuery.isError ? (
        <Alert
          type="error"
          showIcon
          message={extractApiErrorMessage(summaryQuery.error, t('ui.common.error_occurred', 'An error occurred'))}
        />
      ) : null}
      {summary?.mode === 'dry_run' ? (
        <Alert type="warning" showIcon message={t('ui.lapsed.mode_dry_run', 'Dry-run — simulated rows, discarded at go-live.')} />
      ) : null}
      {summary?.mode === 'off' ? (
        <Alert type="info" showIcon message={t('ui.lapsed.mode_off', 'Off — no reminders are sent.')} />
      ) : null}
      {/* Staff contact guidance (§9.1). Staff may call; this never reaches a customer. */}
      <Alert type="info" message={t('ui.lapsed.call_hours', 'Call customers only between 09:00 and 18:00.')} />

      {metrics ? (
        <Card size="small" title={t('ui.lapsed.metrics_title', 'Last 30 days')} data-testid="lapsed-metrics">
          <Row gutter={[16, 16]}>
            <Col xs={12} md={6}><Statistic title={t('ui.lapsed.metric.lapses_started', 'Lapses started')} value={metrics.lapses_started} /></Col>
            <Col xs={12} md={6}><Statistic title={`${sentLabel} · ${t('ui.lapsed.channel.telegram', 'Telegram')}`} value={metrics.reminders_sent.telegram} /></Col>
            <Col xs={12} md={6}><Statistic title={`${sentLabel} · ${t('ui.lapsed.channel.email', 'Email')}`} value={metrics.reminders_sent.email} /></Col>
            <Col xs={12} md={6}><Statistic title={t('ui.lapsed.metric.reordered_after_reminder', 'Reordered after a reminder')} value={metrics.reordered_after_reminder} /></Col>
            <Col xs={12} md={6}><Statistic title={t('ui.lapsed.metric.place_resupplied', 'Place resupplied by another member')} value={metrics.place_resupplied} /></Col>
            <Col xs={12} md={6}><Statistic title={t('ui.lapsed.metric.reordered_before_contact', 'Reordered before contact')} value={metrics.reordered_before_contact} /></Col>
            <Col xs={12} md={6}><Statistic title={t('ui.lapsed.metric.stuck_order_delivered', 'Stuck order delivered')} value={metrics.stuck_order_delivered} /></Col>
            <Col xs={12} md={6}><Statistic title={t('ui.lapsed.metric.switched_off_by_customers', 'Switched off by customers')} value={metrics.switched_off_by_customers} /></Col>
            <Col xs={12} md={6}><Statistic title={t('ui.lapsed.metric.blocks_after_reminder', 'Bot blocks within 3 days of a reminder')} value={metrics.blocks_after_reminder} /></Col>
          </Row>
          <Space wrap style={{ marginTop: 12 }}>
            <Text type="secondary">{t('ui.lapsed.metric.escalated_by_flag', 'Escalated, by reason')}</Text>
            {(summary.flags || []).map((value) => (
              <Tag key={value} color={flagColor(value)}>
                {`${t(`ui.lapsed.flag.${value}`, value)}: ${publishedValue(metrics.escalated_by_flag, value) ?? 0}`}
              </Tag>
            ))}
          </Space>
        </Card>
      ) : null}

      {summary ? (
        <Card size="small" title={t('ui.lapsed.settings_title', 'Reminder settings (read-only)')} data-testid="lapsed-settings">
          <Descriptions size="small" column={{ xs: 1, sm: 2, md: 3 }}>
            {summary.settings.map((entry) => (
              <Descriptions.Item key={entry.key} label={t(`ui.lapsed.settings.${entry.key}`, entry.key)}>
                {String(entry.value)}
              </Descriptions.Item>
            ))}
          </Descriptions>
        </Card>
      ) : null}

      <Card size="small">
        <Space direction="vertical" style={{ width: '100%' }}>
          <Space wrap>
            <Segmented value={tab} options={tabOptions} onChange={chooseTab} />
            <Input.Search
              allowClear
              style={{ minWidth: 260 }}
              placeholder={t('ui.lapsed.search_placeholder', 'Search by name or phone')}
              onSearch={applySearch}
            />
          </Space>
          {FLAGGED_TABS.includes(tab) ? (
            <Space wrap data-testid="lapsed-flag-chips">
              {(summary?.flags || []).map((value) => (
                <Tag.CheckableTag
                  key={value}
                  checked={flag === value}
                  onChange={(checked) => applyFlag(checked ? value : null)}
                >
                  {t(`ui.lapsed.flag.${value}`, value)}
                </Tag.CheckableTag>
              ))}
            </Space>
          ) : null}
        </Space>
      </Card>

      {listQuery.isError ? (
        <Alert type="error" showIcon message={extractApiErrorMessage(listQuery.error, t('ui.common.error_occurred', 'An error occurred'))} />
      ) : null}

      <Table
        rowKey="episode_id"
        columns={columns}
        dataSource={listing?.items || []}
        loading={listQuery.isFetching}
        locale={{ emptyText: t('ui.lapsed.empty', 'Nothing here.') }}
        pagination={{
          current: page,
          pageSize: DEFAULT_PAGE_SIZE,
          total: listing?.meta?.total || 0,
          showSizeChanger: false,
          onChange: setPage,
        }}
        scroll={{ x: 1430 }}
      />

      <Modal
        open={Boolean(resolving)}
        title={t('ui.lapsed.resolve_title', 'Record the outcome')}
        okText={t('ui.lapsed.action.resolve', 'Record outcome')}
        okButtonProps={{ disabled: !outcome }}
        confirmLoading={resolveMutation.isPending}
        onOk={() => resolveMutation.mutate({ episodeId: resolving.episode_id, chosenOutcome: outcome, text: note })}
        onCancel={closeResolve}
        destroyOnHidden
      >
        {resolving?.contact_restriction ? (
          <Alert type="warning" showIcon style={{ marginBottom: 12 }} message={restrictionText(resolving.contact_restriction)} />
        ) : null}
        <Text strong>{t('ui.lapsed.resolve_outcome', 'Outcome')}</Text>
        {resolveOptions.length === 0 ? (
          // The summary failed or is still loading: the vocabulary is the backend's, so there is nothing to offer.
          <Alert
            type="warning"
            showIcon
            style={{ margin: '8px 0 12px' }}
            message={t('ui.lapsed.outcomes_unavailable', 'The list of outcomes is not available. Close this window and reload the page to try again.')}
          />
        ) : (
          <Radio.Group
            value={outcome}
            onChange={({ target }) => setOutcome(target.value)}
            style={{ display: 'flex', flexDirection: 'column', gap: 6, margin: '8px 0 12px' }}
          >
            {resolveOptions.map((option) => (
              <Radio key={option.value} value={option.value}>{option.label}</Radio>
            ))}
          </Radio.Group>
        )}
        <label htmlFor="lapsed-outcome-note">{t('ui.lapsed.resolve_note', 'Note (optional)')}</label>
        {/* No maxLength: the backend strips the note and then caps it, so a padded note it accepts
            must not be cut here. An over-long one is refused by the backend and toasted. */}
        <Input.TextArea id="lapsed-outcome-note" rows={3} value={note} onChange={({ target }) => setNote(target.value)} />
      </Modal>

      <Modal
        open={Boolean(turningOff)}
        title={t('ui.lapsed.turn_off_title', 'Turn off reorder reminders')}
        okText={t('ui.lapsed.action.turn_off', 'Turn off reminders')}
        okButtonProps={{ danger: true }}
        confirmLoading={turnOffMutation.isPending}
        onOk={confirmTurnOff}
        onCancel={closeTurnOff}
        destroyOnHidden
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Text type="secondary">
            {t('ui.lapsed.turn_off_hint', 'The customer gets no more reorder reminders. Order and delivery updates continue.')}
          </Text>
          <label htmlFor="lapsed-turn-off-reason">{t('ui.lapsed.turn_off_reason', 'Reason')}</label>
          <Input.TextArea id="lapsed-turn-off-reason" rows={3} maxLength={500} value={reason} onChange={({ target }) => setReason(target.value)} />
        </Space>
      </Modal>
    </Space>
  );
};

export default LapsedCustomers;
