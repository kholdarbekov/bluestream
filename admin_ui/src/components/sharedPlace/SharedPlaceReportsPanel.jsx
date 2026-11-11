import React, { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Card, Drawer, Radio, Space, Table, Tag } from 'antd';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import adminService from '../../services/adminService';
import SharedPlaceCaseDetail, { driverSaidText } from './SharedPlaceCaseDetail';
import { formatLocaleDateTime } from '../../utils/dateUtils';
import { DEFAULT_PAGE_SIZE, SHARED_PLACE_CASES_KEY } from '../../utils/constants';

const STATUS_COPY = new Map([
  ['awaiting_driver', ['ui.users.shared_place_reports.status_awaiting_driver', 'Waiting for the driver', 'gold']],
  ['awaiting_admin', ['ui.users.shared_place_reports.status_awaiting_admin', 'Waiting for an admin', 'red']],
  ['resolved', ['ui.users.shared_place_reports.status_resolved', 'Resolved', 'green']],
  ['closed', ['ui.users.shared_place_reports.status_closed', 'Closed', 'default']],
]);

const statusText = (t, status) => {
  const copy = STATUS_COPY.get(status);
  return copy ? t(copy[0], copy[1]) : status;
};

/**
 * The Users page's "Driver reports" tab (spec 2026-11-10 §4.5): shared-place
 * cases, open ones first (the backend orders them), with a filter for resolved
 * and closed ones. A row opens the case in a drawer.
 *
 * The open case lives in the URL as `case=<id>`, next to the page's `tab`, so
 * the Slack alert's link (`/users?tab=driver-reports&case=<id>`) opens it.
 */
export default function SharedPlaceReportsPanel({ onViewUser }) {
  const { t } = useTranslation('users');
  const [searchParams, setSearchParams] = useSearchParams();
  const [status, setStatus] = useState('open');
  const [page, setPage] = useState(1);

  const caseParam = Number(searchParams.get('case'));
  const openCaseId = Number.isInteger(caseParam) && caseParam > 0 ? caseParam : null;

  const setCaseParam = (caseId) =>
    setSearchParams((previous) => {
      const next = new URLSearchParams(previous);
      if (caseId == null) next.delete('case');
      else next.set('case', String(caseId));
      return next;
    });

  const listQuery = useQuery({
    queryKey: [...SHARED_PLACE_CASES_KEY, 'list', status, page],
    queryFn: () => adminService.listSharedPlaceCases({ status, page, perPage: DEFAULT_PAGE_SIZE }),
    placeholderData: keepPreviousData,
  });
  const list = listQuery.data;

  const columns = [
    {
      title: t('ui.users.shared_place_reports.col_case', 'Case'),
      dataIndex: 'id',
      key: 'id',
      render: (id) => `#${id}`,
    },
    {
      title: t('ui.users.shared_place_reports.col_opened', 'Opened'),
      dataIndex: 'created_at',
      key: 'created_at',
      render: (value) => formatLocaleDateTime(value),
    },
    {
      title: t('ui.users.shared_place_reports.col_order', 'Order'),
      dataIndex: 'order_number',
      key: 'order_number',
    },
    {
      title: t('ui.users.shared_place_reports.col_customer', 'Customer'),
      key: 'customer',
      render: (_, row) => [row.customer_first_name, row.customer_last_name].filter(Boolean).join(' '),
    },
    {
      title: t('ui.users.shared_place_reports.col_address', 'Address'),
      dataIndex: 'address_title',
      key: 'address_title',
    },
    {
      title: t('ui.users.shared_place_reports.col_returned', 'Empties handed back'),
      dataIndex: 'returned_qty',
      key: 'returned_qty',
    },
    {
      title: t('ui.users.shared_place_reports.col_driver', 'Driver'),
      key: 'driver',
      render: (_, row) => driverSaidText(t, row.driver_status, row.answer_kind),
    },
    {
      title: t('ui.users.shared_place_reports.col_status', 'Status'),
      dataIndex: 'status',
      key: 'status',
      render: (value) => <Tag color={STATUS_COPY.get(value)?.[2]}>{statusText(t, value)}</Tag>,
    },
  ];

  return (
    <Card>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Radio.Group
          optionType="button"
          value={status}
          onChange={(event) => {
            setStatus(event.target.value);
            setPage(1);
          }}
          options={[
            { value: 'open', label: t('ui.users.shared_place_reports.filter_open', 'Open') },
            { value: 'resolved', label: statusText(t, 'resolved') },
            { value: 'closed', label: statusText(t, 'closed') },
          ]}
        />
        <Table
          rowKey="id"
          className="admin-table"
          size="middle"
          loading={listQuery.isLoading}
          columns={columns}
          dataSource={list?.cases || []}
          locale={{ emptyText: t('ui.users.shared_place_reports.empty', 'No driver reports') }}
          onRow={(row) => ({ onClick: () => setCaseParam(row.id), style: { cursor: 'pointer' } })}
          pagination={{
            current: page,
            pageSize: DEFAULT_PAGE_SIZE,
            total: list?.total || 0,
            showSizeChanger: false,
            onChange: setPage,
          }}
          scroll={{ x: 'max-content' }}
        />
      </Space>
      <Drawer
        open={openCaseId != null}
        title={openCaseId != null ? t('ui.users.shared_place_reports.case_title', 'Case #{{id}}', { id: openCaseId }) : null}
        width={760}
        onClose={() => setCaseParam(null)}
        destroyOnHidden
      >
        {openCaseId != null && <SharedPlaceCaseDetail caseId={openCaseId} onViewUser={onViewUser} />}
      </Drawer>
    </Card>
  );
}
