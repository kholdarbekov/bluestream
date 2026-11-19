import React, { useState } from 'react';
import { Alert, Button, Card, DatePicker, Select, Space, Table, Tag, Typography } from 'antd';
import { PaperClipOutlined } from '@ant-design/icons';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import driverExpenseService from '../services/driverExpenseService';
import staffService from '../services/staffService';
import DecideExpenseModal from '../components/driverExpenses/DecideExpenseModal';
import {
  EXPENSE_CATEGORIES, EXPENSE_STATUSES, categoryLabel, statusLabel,
} from '../components/driverExpenses/expenseCopy';
import { formatDateTime } from '../utils/dateUtils';
import { formatMoney } from '../utils/formatMoney';

const { Title } = Typography;
const { RangePicker } = DatePicker;
const PER_PAGE = 20;

// Driver cash expenses (spec 2026-11-17 §6.8): the claims drivers send from the staff bot, waiting for a decision. The
// backend publishes every flag, limit and reason on each row, so the page only draws them.
const DriverExpenses = () => {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const [status, setStatus] = useState('pending');
  const [driverId, setDriverId] = useState(null);
  const [category, setCategory] = useState(null);
  const [period, setPeriod] = useState(null);
  const [page, setPage] = useState(1);
  const openId = searchParams.get('id');

  const filters = {
    status,
    driverId: driverId ?? undefined,
    category: category ?? undefined,
    startDate: period?.[0]?.format('YYYY-MM-DD'),
    endDate: period?.[1]?.format('YYYY-MM-DD'),
  };
  const list = useQuery({
    queryKey: ['driverExpenses', 'list', filters, page],
    queryFn: () => driverExpenseService.list({ ...filters, page, perPage: PER_PAGE }),
    placeholderData: keepPreviousData,
  });
  const drivers = useQuery({
    queryKey: ['driverExpenses', 'drivers'],
    queryFn: () => staffService.getDeliveryPersons({ per_page: 100 }),
    staleTime: 5 * 60_000,
  });
  // `?id=` opens one claim whatever the filters show, so the dialog reads its own claim.
  const detail = useQuery({
    queryKey: ['driverExpenses', 'detail', openId],
    queryFn: () => driverExpenseService.get(openId),
    enabled: Boolean(openId),
  });

  const refilter = (setter) => (value) => { setter(value); setPage(1); };
  const closeDialog = () => setSearchParams({}, { replace: true });
  const driverOptions = (drivers.data?.data?.data?.items || []).map((driver) => ({
    value: driver.user_id, label: driver.full_name || driver.name,
  }));

  const columns = [
    { title: t('ui.driver_expenses.col_recorded', 'Recorded'), dataIndex: 'submitted_at', render: (v) => formatDateTime(v) },
    { title: t('ui.driver_expenses.col_driver', 'Driver'), dataIndex: 'driver_name' },
    { title: t('ui.driver_expenses.col_trip', 'Trip'), dataIndex: 'driver_cash_session_id', render: (id) => `#${id}` },
    { title: t('ui.driver_expenses.col_category', 'Category'), dataIndex: 'category', render: (c) => categoryLabel(t, c) },
    { title: t('ui.driver_expenses.col_claimed', 'Claimed'), dataIndex: 'amount', align: 'right', render: (v) => formatMoney(v) },
    {
      title: t('ui.driver_expenses.col_approved', 'Approved'),
      dataIndex: 'approved_amount',
      align: 'right',
      render: (v) => (v == null ? '—' : formatMoney(v)),
    },
    { title: t('ui.driver_expenses.col_note', 'Note'), dataIndex: 'note', ellipsis: true },
    {
      title: t('ui.driver_expenses.col_receipt', 'Receipt'),
      dataIndex: 'has_receipt',
      render: (has) => (has ? <PaperClipOutlined /> : '—'),
    },
    {
      title: t('ui.driver_expenses.col_status', 'Status'),
      dataIndex: 'status',
      render: (value, row) => (
        <Space size={4}>
          {statusLabel(t, value)}
          {row.self_decided && <Tag>{t('ui.driver_expenses.self_decided', 'Self-decided')}</Tag>}
        </Space>
      ),
    },
    { title: t('ui.driver_expenses.col_decided_by', 'Decided by'), dataIndex: 'decided_by_name', render: (v) => v || '—' },
    {
      title: '',
      key: 'open',
      render: (_, row) => (
        <Button size="small" onClick={() => setSearchParams({ id: String(row.id) })}>
          {t('ui.driver_expenses.review', 'Review')}
        </Button>
      ),
    },
  ];

  return (
    <div>
      <Title level={3}>{t('ui.driver_expenses.title', 'Driver expenses')}</Title>
      <Space wrap style={{ marginBottom: 16 }}>
        <Select
          aria-label={t('ui.driver_expenses.col_status', 'Status')}
          value={status}
          onChange={refilter(setStatus)}
          style={{ width: 160 }}
          options={[...EXPENSE_STATUSES, 'all'].map((value) => ({ value, label: statusLabel(t, value) }))}
        />
        <Select
          allowClear
          showSearch
          optionFilterProp="label"
          placeholder={t('ui.driver_expenses.col_driver', 'Driver')}
          value={driverId}
          onChange={refilter(setDriverId)}
          style={{ width: 200 }}
          options={driverOptions}
        />
        <Select
          allowClear
          placeholder={t('ui.driver_expenses.col_category', 'Category')}
          value={category}
          onChange={refilter(setCategory)}
          style={{ width: 200 }}
          options={EXPENSE_CATEGORIES.map((value) => ({ value, label: categoryLabel(t, value) }))}
        />
        <RangePicker
          aria-label={t('ui.driver_expenses.col_recorded', 'Recorded')}
          value={period}
          onChange={refilter(setPeriod)}
        />
      </Space>
      {list.isError && <Alert type="error" message={t('ui.driver_expenses.load_failed', 'Could not load the expenses')} />}
      {list.data?.totals_by_category && (
        <Card size="small" title={t('ui.driver_expenses.totals_title', 'Approved spend by category')} style={{ marginBottom: 16 }}>
          <Space wrap size="large">
            {list.data.totals_by_category.map((row) => (
              <span key={row.category}>{categoryLabel(t, row.category)}: {formatMoney(row.approved_total)}</span>
            ))}
          </Space>
        </Card>
      )}
      <Table
        rowKey="id"
        columns={columns}
        dataSource={list.data?.items || []}
        loading={list.isLoading}
        scroll={{ x: true }}
        pagination={{
          current: page,
          pageSize: PER_PAGE,
          total: list.data?.total || 0,
          showSizeChanger: false,
          onChange: setPage,
        }}
      />
      {openId && detail.data?.expense && (
        <DecideExpenseModal
          key={detail.data.expense.id}
          detail={detail.data}
          reasonMinLength={detail.data.reason_min_length ?? list.data?.reason_min_length}
          onClose={closeDialog}
        />
      )}
    </div>
  );
};

export default DriverExpenses;
