import React, { useState } from 'react';
import { Alert, Descriptions, Drawer, Space, Spin, Steps, Table, Tag, Typography } from 'antd';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import adminService from '../../services/adminService';
import { DEFAULT_PAGE_SIZE, PRICE_CHANGES_KEY } from '../../utils/constants';
import { formatCalendarDate, formatDateTime } from '../../utils/dateUtils';
import { formatMoneyUZS } from '../../utils/formatMoney';
import { PriceChangeActions } from './PriceChangeFormModal';

const { Text, Title } = Typography;

// The step the timeline is on, a lookup from the published `stage` alone: the page never reads `status` or a date to
// decide it. `window_closed` is past the last step (everything done).
const CURRENT_STEP = new Map([
  ['scheduled', 0], ['announcing', 0], ['announced', 1], ['reminded', 2], ['applied', 3], ['window_closed', 4],
  ['cancelled', 0],
]);
const STAGE_COLOR = new Map([
  ['scheduled', 'default'], ['announcing', 'processing'], ['announced', 'blue'], ['reminded', 'blue'],
  ['applied', 'green'], ['window_closed', 'default'], ['cancelled', 'red'],
]);

// One literal `t(...)` per stage, so the key scan and the seed see each of them.
export const useStageLabels = () => {
  const { t } = useTranslation();
  return new Map([
    ['scheduled', t('ui.price_changes.stage.scheduled', 'Scheduled')],
    ['announcing', t('ui.price_changes.stage.announcing', 'Announcing')],
    ['announced', t('ui.price_changes.stage.announced', 'Announced')],
    ['reminded', t('ui.price_changes.stage.reminded', 'Reminder sent')],
    ['applied', t('ui.price_changes.stage.applied', 'New prices in effect')],
    ['window_closed', t('ui.price_changes.stage.window_closed', 'Old-price window closed')],
    ['cancelled', t('ui.price_changes.stage.cancelled', 'Cancelled')],
  ]);
};

export const StageTag = ({ stage }) => {
  const labels = useStageLabels();
  return <Tag color={STAGE_COLOR.get(stage)}>{labels.get(stage) ?? stage}</Tag>;
};

const dash = '-';
const show = (value) => (value === null || value === undefined || value === '' ? dash : value);

const Section = ({ label, children }) => (
  <section aria-label={label} style={{ marginTop: 24 }}>
    <Title level={5}>{label}</Title>
    {children}
  </section>
);

const Timeline = ({ priceChange }) => {
  const { t } = useTranslation();
  const done = (at) => (at ? (
    <div>{t('ui.price_changes.step_done', 'Done {{at}}', { at: formatDateTime(at) })}</div>
  ) : null);
  const items = [
    {
      title: t('ui.price_changes.step_announcement', 'Announcement'),
      description: (<><div>{formatCalendarDate(priceChange.announce_on)}</div>{done(priceChange.announcement_started_at)}</>),
    },
    {
      title: t('ui.price_changes.step_reminder', 'Reminder'),
      description: (<><div>{formatCalendarDate(priceChange.reminder_on)}</div>{done(priceChange.reminder_started_at)}</>),
    },
    {
      title: t('ui.price_changes.step_new_prices', 'New prices'),
      description: (<><div>{formatCalendarDate(priceChange.change_on)}</div>{done(priceChange.applied_at)}</>),
    },
    {
      title: t('ui.price_changes.step_old_price_orders', 'Old-price orders until'),
      description: <div>{formatCalendarDate(priceChange.courtesy_last_day)}</div>,
    },
  ];
  const stage = priceChange.stage;
  let status;
  if (stage === 'cancelled') status = 'error';
  else if (stage === 'announcing') status = 'process';
  return <Steps direction="vertical" size="small" current={CURRENT_STEP.get(stage)} status={status} items={items} />;
};

const ProductsTable = ({ items }) => {
  const { t } = useTranslation();
  const columns = [
    { title: t('ui.price_changes.col_product', 'Product'), dataIndex: 'product_name' },
    {
      title: t('ui.price_changes.col_price_now', 'Price now'),
      dataIndex: 'current_price',
      render: (value) => formatMoneyUZS(value),
    },
    {
      title: t('ui.price_changes.col_new_price', 'New price'),
      dataIndex: 'new_price',
      render: (value) => formatMoneyUZS(value),
    },
    {
      title: t('ui.price_changes.col_old_price', 'Old price'),
      dataIndex: 'old_price',
      render: (value) => (value === null || value === undefined ? dash : formatMoneyUZS(value)),
    },
    { title: t('ui.price_changes.col_cap', 'Old-price units per order'), dataIndex: 'courtesy_cap' },
  ];
  return <Table size="small" rowKey="product_id" columns={columns} dataSource={items} pagination={false} />;
};

const NoticeCounts = ({ counts }) => {
  const { t } = useTranslation();
  const rows = [
    { key: 'announcement', label: t('ui.price_changes.notice_announcement', 'Announcement'), ...counts.announcement },
    { key: 'reminder', label: t('ui.price_changes.notice_reminder', 'Reminder'), ...counts.reminder },
  ];
  const columns = [
    { title: t('ui.price_changes.col_notice', 'Notice'), dataIndex: 'label' },
    { title: t('ui.price_changes.col_telegram', 'Telegram'), dataIndex: 'telegram' },
    { title: t('ui.price_changes.col_email', 'Email'), dataIndex: 'email' },
    { title: t('ui.price_changes.col_none', 'Could not reach'), dataIndex: 'none' },
    { title: t('ui.price_changes.col_queued', 'Waiting to send'), dataIndex: 'queued' },
  ];
  return (
    <>
      <Table size="small" rowKey="key" columns={columns} dataSource={rows} pagination={false} />
      <Descriptions
        size="small"
        column={1}
        style={{ marginTop: 12 }}
        items={[
          { key: 'eligible', label: t('ui.price_changes.eligible', 'Customers who keep their price'), children: counts.eligible },
          { key: 'used', label: t('ui.price_changes.courtesy_used', 'Orders that used it'), children: counts.courtesy_used },
          { key: 'exceptions', label: t('ui.price_changes.exceptions', 'Exceptions granted'), children: counts.exceptions },
        ]}
      />
    </>
  );
};

const UnreachableTable = ({ priceChangeId }) => {
  const { t } = useTranslation();
  const [page, setPage] = useState(1);
  const { data, isFetching } = useQuery({
    queryKey: [...PRICE_CHANGES_KEY, priceChangeId, 'unreachable', page],
    queryFn: () => adminService.listPriceChangeUnreachable(priceChangeId, { page, perPage: DEFAULT_PAGE_SIZE }),
    placeholderData: keepPreviousData,
  });
  const columns = [
    { title: t('ui.price_changes.col_name', 'Name'), dataIndex: 'name', render: show },
    { title: t('ui.price_changes.col_phone', 'Phone'), dataIndex: 'phone', render: show },
  ];
  return (
    <>
      <Text type="secondary">
        {t('ui.price_changes.unreachable_hint', 'Call them between 09:00 and 18:00, or ask the driver to tell them at the door.')}
      </Text>
      <Table
        size="small"
        style={{ marginTop: 8 }}
        rowKey="user_id"
        columns={columns}
        dataSource={data?.items || []}
        loading={isFetching}
        pagination={{
          current: page,
          pageSize: DEFAULT_PAGE_SIZE,
          total: data?.meta?.total || 0,
          showSizeChanger: false,
          onChange: setPage,
        }}
      />
    </>
  );
};

const ExceptionsTable = ({ priceChangeId }) => {
  const { t } = useTranslation();
  const { data, isFetching } = useQuery({
    queryKey: [...PRICE_CHANGES_KEY, priceChangeId, 'exceptions'],
    queryFn: () => adminService.listPriceChangeExceptions(priceChangeId),
  });
  const columns = [
    {
      title: t('ui.price_changes.col_customer', 'Customer'),
      dataIndex: 'customer_name',
      render: (name, row) => (
        <Space direction="vertical" size={0}>
          <span>{show(name)}</span>
          <Text type="secondary">{show(row.customer_phone)}</Text>
        </Space>
      ),
    },
    { title: t('ui.price_changes.col_reason', 'Reason'), dataIndex: 'reason', render: show },
    { title: t('ui.price_changes.col_granted_by', 'Granted by'), dataIndex: 'granted_by_name', render: show },
    {
      title: t('ui.price_changes.col_granted_at', 'Granted'),
      dataIndex: 'granted_at',
      render: (at) => formatDateTime(at),
    },
    { title: t('ui.price_changes.col_used_order', 'Used by order'), dataIndex: 'used_order_number', render: show },
  ];
  return (
    <Table size="small" rowKey="id" columns={columns} dataSource={data || []} loading={isFetching} pagination={false} />
  );
};

const PriceChangeDrawer = ({ priceChangeId, onClose }) => {
  const { t } = useTranslation();
  const { data, isLoading, isError } = useQuery({
    queryKey: [...PRICE_CHANGES_KEY, priceChangeId],
    queryFn: () => adminService.getPriceChange(priceChangeId),
    enabled: priceChangeId != null,
  });
  const priceChange = data?.price_change;
  const productsLabel = t('ui.price_changes.products', 'Products');
  const noticesLabel = t('ui.price_changes.notices', 'Notices');

  return (
    <Drawer
      open={priceChangeId != null}
      onClose={onClose}
      width={720}
      title={t('ui.price_changes.drawer_title', 'Price change')}
      destroyOnClose
    >
      {isLoading && <Spin />}
      {isError && <Alert type="error" message={t('ui.price_changes.load_failed', 'Could not load this price change')} />}
      {priceChange && (
        <>
          <Space direction="vertical" size={4}>
            <StageTag stage={priceChange.stage} />
            <PriceChangeActions priceChange={priceChange} size="middle" />
            {priceChange.stage === 'cancelled' && (
              <Text type="secondary">
                {t('ui.price_changes.cancelled_by', 'Cancelled by {{name}} on {{at}}', {
                  name: show(priceChange.cancelled_by_name),
                  at: formatDateTime(priceChange.cancelled_at),
                })}
              </Text>
            )}
          </Space>
          <Section label={t('ui.price_changes.timeline', 'Timeline')}>
            <Timeline priceChange={priceChange} />
          </Section>
          <Section label={productsLabel}>
            <ProductsTable items={priceChange.items} />
          </Section>
          <Section label={noticesLabel}>
            <NoticeCounts counts={data.counts} />
          </Section>
          <Section label={t('ui.price_changes.unreachable', 'Customers we could not reach')}>
            <UnreachableTable priceChangeId={priceChangeId} />
          </Section>
          <Section label={t('ui.price_changes.exceptions_title', 'Manager exceptions')}>
            <ExceptionsTable priceChangeId={priceChangeId} />
          </Section>
        </>
      )}
    </Drawer>
  );
};

export default PriceChangeDrawer;
