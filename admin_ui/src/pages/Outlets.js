import React, { useMemo, useState } from 'react';
import {
  Alert, Card, Table, Tag, Space, Button, Input, Select, Row, Col, Statistic, Drawer, Descriptions, Typography,
  message, Modal, Form, Tabs, InputNumber, Popconfirm, TimePicker,
} from 'antd';
import { SearchOutlined, ReloadOutlined, ImportOutlined, TeamOutlined } from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import salesService from '../services/salesService';
import staffService from '../services/staffService';
import api from '../services/api';
import { fetchAllPages } from '../utils/pagination';
import { BULK_LOAD_PAGE_SIZE, DEFAULT_PAGE_SIZE } from '../utils/constants';
import { extractApiErrorMessage } from '../utils/apiError';
import dayjs from 'dayjs';
import customParseFormat from 'dayjs/plugin/customParseFormat';

dayjs.extend(customParseFormat);

const { Title, Text } = Typography;

// Mirror business_app/models/sales.py — OUTLET_STAGES, OUTLET_TYPES, OUTLET_CLASSES, LOST_REASONS.
const STAGES = ['prospect', 'trial', 'activation_requested', 'active', 'at_risk', 'dormant', 'lost'];
const STAGE_COLORS = {
  prospect: 'default', trial: 'purple', activation_requested: 'gold', active: 'green', at_risk: 'orange', dormant: 'blue', lost: 'red',
};
const STAGE_LABELS = {
  prospect: 'Prospect', trial: 'Trial', activation_requested: 'Awaiting activation', active: 'Active', at_risk: 'At risk', dormant: 'Dormant', lost: 'Lost',
};
const OUTLET_TYPES = ['grocery_store', 'workplace', 'individual'];
const OUTLET_CLASSES = ['A', 'B', 'C'];
const LOST_REASONS = ['price', 'has_supplier', 'no_space', 'owner_absent', 'low_footfall', 'payment_terms', 'quality', 'closed', 'not_reached', 'other'];

// The wire shape of both window columns: `serialize_outlet` publishes "HH:MM" and
// `parse_window_time` (time.fromisoformat) reads it back. Same string, both directions.
const WINDOW_FORMAT = 'HH:mm';

const Outlets = () => {
  const { t, i18n } = useTranslation(['sales_agents', 'common']);
  const queryClient = useQueryClient();
  const [rejectForm] = Form.useForm();
  const [lostForm] = Form.useForm();
  const [bulkForm] = Form.useForm();

  const [pagination, setPagination] = useState({ page: 1, per_page: DEFAULT_PAGE_SIZE });
  const [search, setSearch] = useState('');
  const [stage, setStage] = useState();
  const [outletClass, setOutletClass] = useState();
  const [outletType, setOutletType] = useState();
  const [agentId, setAgentId] = useState();
  const [district, setDistrict] = useState();
  const [unvisitedDays, setUnvisitedDays] = useState();

  const [selectedId, setSelectedId] = useState(null);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [lostOpen, setLostOpen] = useState(false);
  const [bulkOpen, setBulkOpen] = useState(false);
  const [windowOpen, setWindowOpen] = useState(false);
  const [drawerTab, setDrawerTab] = useState('overview');

  const narrow = (setter) => (value) => { setter(value); setPagination((p) => ({ ...p, page: 1 })); };

  const filters = useMemo(() => ({
    page: pagination.page,
    per_page: pagination.per_page,
    search: search || undefined,
    stage: stage || undefined,
    class: outletClass || undefined,
    outlet_type: outletType || undefined,
    agent_user_id: agentId || undefined,
    district: district || undefined,
    unvisited_days: unvisitedDays || undefined,
  }), [pagination, search, stage, outletClass, outletType, agentId, district, unvisitedDays]);

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['outlets', filters],
    queryFn: () => salesService.getOutlets(filters),
    placeholderData: keepPreviousData,
  });

  const { data: detail } = useQuery({
    queryKey: ['outlet', selectedId],
    queryFn: () => salesService.getOutlet(selectedId),
    enabled: Boolean(selectedId),
  });

  // The drawer's *Visits* tab and the Visits page are the SAME route, narrowed by `outlet_id`
  // (R11). 90 inclusive days is a UI choice that sits inside the backend's date-range cap
  // (SALES_METRICS_MAX_RANGE_DAYS, default 92) — if that cap is ever lowered below 90, this
  // window has to move with it or the tab answers 400.
  const outletVisitFilters = useMemo(() => ({
    outlet_id: selectedId,
    start_date: dayjs().subtract(89, 'day').format('YYYY-MM-DD'),
    end_date: dayjs().format('YYYY-MM-DD'),
    page: 1,
    per_page: DEFAULT_PAGE_SIZE,
  }), [selectedId]);

  // Keyed ['visits', 'outlet', selectedId] — one cache entry per outlet the session has opened,
  // so re-opening a drawer paints from cache and two outlets never share a result. Keyed by the
  // outlet ID rather than by the whole filter object on purpose: `start_date` rolls at local
  // midnight and a filter-shaped key would mint a second entry per outlet per day. The page's
  // own list (['visits', filters]) is a different entry and is never disturbed by this one.
  //
  // Realtime invalidation does NOT reach these keys, and never has: `AdminLayout.js` lists
  // query names as bare STRINGS, and react-query v5's `partialMatchKey` compares a string
  // filter against an array key type-first and bails — so none of the 17 entries in that list
  // matches anything. That is a pre-existing estate-wide defect (V05), parked to the backlog;
  // this drawer refetches when it is re-opened, not when a check-in lands in the field. If V05
  // is fixed by passing arrays, `['visits']` will match this key by prefix, which is the
  // behaviour we want — nothing here needs to change for that.
  const outletVisitsQuery = useQuery({
    queryKey: ['visits', 'outlet', selectedId],
    queryFn: () => salesService.getVisits(outletVisitFilters),
    enabled: Boolean(selectedId) && drawerTab === 'visits',
  });

  const { data: agentsData } = useQuery({
    queryKey: ['salesAgentOptions'],
    queryFn: () => fetchAllPages(
      (page) => staffService.getSalesAgents({ page, per_page: BULK_LOAD_PAGE_SIZE }),
      (resp) => resp?.data?.data?.items || [],
      BULK_LOAD_PAGE_SIZE,
    ),
    staleTime: 60_000,
  });
  const agents = agentsData || [];

  // Every district picker on this page — filter, drawer and bulk assign — is fed from geo-config,
  // NOT from the districts present in the loaded rows. Two reasons: its `key`s are exactly the
  // TASHKENT_DISTRICTS keys the write paths validate against (`OutletService.update`,
  // `bulk_assign_by_district`), and a list-derived picker could only ever offer the districts on
  // the current page — which excludes the empty district an admin most needs to bulk-assign.
  // Same query key as SalesAgents.js so react-query serves both pages from one fetch.
  const { data: districtsData } = useQuery({
    queryKey: ['geoDistricts', i18n?.language],
    queryFn: () => api.get(`/addresses/geo-config?lang=${i18n?.language || 'en'}`),
    staleTime: 3600_000,
  });
  const districts = useMemo(() => districtsData?.data?.data?.districts || [], [districtsData]);
  const districtOptions = useMemo(() => districts.map((d) => ({ value: d.key, label: d.name })), [districts]);
  const districtNames = useMemo(() => Object.fromEntries(districts.map((d) => [d.key, d.name])), [districts]);

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['outlets'] });
    if (selectedId) {
      queryClient.invalidateQueries({ queryKey: ['outlet', selectedId] });
    }
  };
  const onError = (err) => message.error(extractApiErrorMessage(err, t('ui.common.error_occurred', 'An error occurred')));

  const approveMutation = useMutation({ mutationFn: ({ id }) => salesService.approveOutlet(id, null), onSuccess: () => { message.success(t('sales_agents:outlet_approved', 'Outlet activated')); refresh(); }, onError });
  const rejectMutation = useMutation({ mutationFn: ({ id, reason }) => salesService.rejectOutlet(id, reason), onSuccess: () => { message.success(t('sales_agents:outlet_rejected', 'Request rejected')); setRejectOpen(false); rejectForm.resetFields(); refresh(); }, onError });
  const assignMutation = useMutation({ mutationFn: ({ id, agentUserId }) => salesService.assignOutlet(id, agentUserId), onSuccess: () => { message.success(t('sales_agents:outlet_assigned', 'Agent assigned')); refresh(); }, onError });
  const updateMutation = useMutation({ mutationFn: ({ id, payload }) => salesService.updateOutlet(id, payload), onSuccess: () => { message.success(t('sales_agents:outlet_updated', 'Outlet updated')); setWindowOpen(false); refresh(); }, onError });
  const lostMutation = useMutation({ mutationFn: ({ id, reason, note }) => salesService.markLost(id, reason, note), onSuccess: () => { message.success(t('sales_agents:outlet_marked_lost', 'Marked as lost')); setLostOpen(false); lostForm.resetFields(); refresh(); }, onError });
  const bulkMutation = useMutation({ mutationFn: ({ district: d, agentUserId }) => salesService.bulkAssign(d, agentUserId), onSuccess: (res) => { message.success(`${t('sales_agents:bulk_assigned', 'Outlets assigned')}: ${res?.updated ?? 0}`); setBulkOpen(false); bulkForm.resetFields(); refresh(); }, onError });
  const importMutation = useMutation({ mutationFn: () => salesService.importExistingCustomers(), onSuccess: (res) => { message.success(`${t('sales_agents:imported', 'Customers imported as outlets')}: ${res?.created ?? 0}`); refresh(); }, onError });

  const outlets = data?.items || [];
  const summary = data?.summary || {};

  const columns = [
    { title: t('sales_agents:outlet_name', 'Outlet'), dataIndex: 'name', key: 'name', render: (text, record) => <a onClick={() => setSelectedId(record.id)}>{text}</a> },
    { title: t('sales_agents:outlet_type', 'Type'), dataIndex: 'outlet_type', key: 'outlet_type' },
    // eslint-disable-next-line security/detect-object-injection
    { title: t('sales_agents:stage', 'Stage'), dataIndex: 'stage', key: 'stage', render: (value) => <Tag color={STAGE_COLORS[value] || 'default'}>{value}</Tag> },
    { title: t('sales_agents:class', 'Class'), dataIndex: 'class', key: 'class', render: (v) => v || '—' },
    // eslint-disable-next-line security/detect-object-injection
    { title: t('sales_agents:district', 'District'), dataIndex: 'district', key: 'district', render: (v) => districtNames[v] || v || '—' },
    { title: t('sales_agents:agent', 'Agent'), dataIndex: 'assigned_agent_name', key: 'agent', render: (v) => v || '—' },
    { title: t('sales_agents:last_visit', 'Last visit'), dataIndex: 'last_visit_at', key: 'last_visit_at', render: (v) => (v ? v.slice(0, 10) : '—') },
  ];

  const outlet = detail?.outlet;
  const canApprove = outlet && ['activation_requested', 'prospect', 'trial'].includes(outlet.stage);

  // Both-or-neither, and explicit nulls to clear. One edge alone is not a window: the order path
  // offers the outlet's window only when BOTH edges are set (visit_service.py:571-580), so half a
  // pair is a write that looks saved and changes nothing. And "cleared" has to travel as null —
  // `_validated_payload`'s exclude_unset leaves an absent key alone.
  const submitWindow = (values) => {
    const start = values.delivery_window_start ? values.delivery_window_start.format(WINDOW_FORMAT) : null;
    const end = values.delivery_window_end ? values.delivery_window_end.format(WINDOW_FORMAT) : null;
    if ((start === null) !== (end === null)) {
      message.error(t('sales_agents:outlets.delivery_window.invalid', 'Set both the start and the end, or clear both.'));
      return;
    }
    updateMutation.mutate({ id: outlet.id, payload: { delivery_window_start: start, delivery_window_end: end } });
  };

  return (
    <div>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Col><Title level={3} style={{ margin: 0 }}>{t('sales_agents:outlets_title', 'Outlets')}</Title></Col>
        <Col>
          <Space>
            <Button icon={<TeamOutlined />} onClick={() => setBulkOpen(true)}>{t('sales_agents:bulk_assign', 'Bulk assign by district')}</Button>
            <Popconfirm title={t('sales_agents:import_confirm', 'Create an outlet for every grocery/workplace customer without one?')} onConfirm={() => importMutation.mutate()}>
              <Button icon={<ImportOutlined />} loading={importMutation.isPending}>{t('sales_agents:import_existing', 'Import existing customers')}</Button>
            </Popconfirm>
          </Space>
        </Col>
      </Row>

      {/* `meta.summary` is estate-wide by design, not a tally of the filtered rows — said out loud
          here, because read as "matching your filter" these tiles would claim 2 prospects in a
          district that returned 1 row. */}
      <Text type="secondary">{t('sales_agents:summary_is_estate_wide', 'Totals across every outlet — the filters below do not narrow them.')}</Text>
      <Row gutter={[16, 16]} style={{ marginTop: 8, marginBottom: 24 }}>
        {STAGES.map((s) => (
          <Col xs={12} sm={6} lg={3} key={s}>
            {/* eslint-disable-next-line security/detect-object-injection */}
            <Card><Statistic title={t(`sales_agents:stage_${s}`, STAGE_LABELS[s])} value={summary[s] || 0} /></Card>
          </Col>
        ))}
      </Row>

      <Card style={{ marginBottom: 16 }}>
        <Row gutter={[16, 16]} align="middle">
          <Col xs={24} sm={6}><Input placeholder={t('sales_agents:search_outlets', 'Search by name or phone')} prefix={<SearchOutlined />} value={search} onChange={(e) => narrow(setSearch)(e.target.value)} allowClear /></Col>
          <Col xs={12} sm={4}><Select placeholder={t('sales_agents:stage', 'Stage')} value={stage} onChange={narrow(setStage)} allowClear style={{ width: '100%' }} options={STAGES.map((s) => ({ value: s, label: s }))} /></Col>
          <Col xs={12} sm={3}><Select placeholder={t('sales_agents:class', 'Class')} value={outletClass} onChange={narrow(setOutletClass)} allowClear style={{ width: '100%' }} options={OUTLET_CLASSES.map((c) => ({ value: c, label: c }))} data-testid="filter-class" /></Col>
          <Col xs={12} sm={4}><Select placeholder={t('sales_agents:outlet_type', 'Type')} value={outletType} onChange={narrow(setOutletType)} allowClear style={{ width: '100%' }} options={OUTLET_TYPES.map((v) => ({ value: v, label: v }))} /></Col>
          <Col xs={12} sm={4}><Select placeholder={t('sales_agents:district', 'District')} value={district} onChange={narrow(setDistrict)} allowClear showSearch optionFilterProp="label" style={{ width: '100%' }} options={districtOptions} /></Col>
          <Col xs={12} sm={4}><Select placeholder={t('sales_agents:agent', 'Agent')} value={agentId} onChange={narrow(setAgentId)} allowClear style={{ width: '100%' }} options={agents.map((a) => ({ value: a.user_id, label: a.full_name }))} /></Col>
          <Col xs={12} sm={3}><InputNumber placeholder={t('sales_agents:unvisited_days', 'Unvisited ≥ days')} min={1} value={unvisitedDays} onChange={narrow(setUnvisitedDays)} style={{ width: '100%' }} /></Col>
          <Col><Button icon={<ReloadOutlined />} onClick={() => refetch()}>{t('ui.common.refresh', 'Refresh')}</Button></Col>
        </Row>
      </Card>

      <Card>
        <Table
          columns={columns}
          dataSource={outlets}
          rowKey="id"
          loading={isLoading}
          pagination={{ current: pagination.page, pageSize: pagination.per_page, total: data?.total || 0, onChange: (page, per_page) => setPagination({ page, per_page }), showSizeChanger: false }}
          scroll={{ x: 900 }}
        />
      </Card>

      <Drawer title={outlet?.name || ''} open={Boolean(selectedId)} onClose={() => { setSelectedId(null); setDrawerTab('overview'); }} width={760}
        extra={outlet ? (
          <Space>
            <Button onClick={() => setWindowOpen(true)}>{t('sales_agents:outlets.delivery_window.edit', 'Edit delivery window')}</Button>
            {canApprove && <Button type="primary" loading={approveMutation.isPending} onClick={() => approveMutation.mutate({ id: outlet.id })}>{t('sales_agents:approve', 'Approve')}</Button>}
            {outlet.stage === 'activation_requested' && <Button danger onClick={() => setRejectOpen(true)}>{t('sales_agents:reject', 'Reject')}</Button>}
            {outlet.stage !== 'lost' && <Button onClick={() => setLostOpen(true)}>{t('sales_agents:mark_lost', 'Mark lost')}</Button>}
          </Space>
        ) : null}
      >
        {outlet ? (
          <Tabs activeKey={drawerTab} onChange={setDrawerTab} items={[
            {
              key: 'overview',
              label: t('sales_agents:tab_overview', 'Overview'),
              children: (
                <Space direction="vertical" size="large" style={{ width: '100%' }}>
                  <Descriptions bordered column={2} size="small">
                    <Descriptions.Item label={t('sales_agents:stage', 'Stage')}><Tag color={STAGE_COLORS[outlet.stage] || 'default'}>{outlet.stage}</Tag></Descriptions.Item>
                    <Descriptions.Item label={t('sales_agents:outlet_type', 'Type')}>{outlet.outlet_type}</Descriptions.Item>
                    <Descriptions.Item label={t('sales_agents:class', 'Class')}>{outlet.class || '—'}</Descriptions.Item>
                    <Descriptions.Item label={t('sales_agents:outlet_address', 'Address')}>{outlet.address_text || '—'}</Descriptions.Item>
                    {/* NULL means "not applicable", and `OutletService.card` is the only place that
                        decides it: no customer account means no wallet, no address row means no
                        bottle ledger. `?? 0` printed "Owes: 0 UZS" on a prospect that has never
                        ordered, which reads as a settled bill. A real 0 still renders as 0. */}
                    <Descriptions.Item label={t('sales_agents:open_receivable', 'Owes')}>{outlet.open_receivable == null ? '—' : `${outlet.open_receivable} UZS`}</Descriptions.Item>
                    <Descriptions.Item label={t('sales_agents:bottle_balance', 'Bottles at outlet')}>{outlet.bottle_balance ?? '—'}</Descriptions.Item>
                    {/* Each edge on its own. The MODAL refuses to save a half pair, but nothing
                        refuses to STORE one (the backend's both-or-neither refusal is backlog and
                        the agent PUT accepts the fields independently), so `09:30` with no end is
                        a reachable row — and collapsing it to a bare dash told the operator there
                        was no window at all, while the modal opened pre-filled with the 09:30 the
                        overview had just denied. Both missing is still one dash: there is nothing
                        to show, not two halves of nothing. */}
                    <Descriptions.Item label={t('sales_agents:outlets.delivery_window.label', 'Delivery window')}>
                      {outlet.delivery_window_start || outlet.delivery_window_end
                        ? `${outlet.delivery_window_start || '—'}–${outlet.delivery_window_end || '—'}`
                        : '—'}
                    </Descriptions.Item>
                    <Descriptions.Item label={t('sales_agents:notes', 'Notes')} span={2}>{outlet.notes || '—'}</Descriptions.Item>
                  </Descriptions>
                  <Space wrap>
                    <span>{t('sales_agents:agent', 'Agent')}:</span>
                    <Select style={{ minWidth: 240 }} value={outlet.assigned_agent_user_id || undefined} allowClear placeholder="—" options={agents.map((a) => ({ value: a.user_id, label: a.full_name }))} onChange={(v) => assignMutation.mutate({ id: outlet.id, agentUserId: v || null })} data-testid="outlet-agent-select" />
                    {/* The one place a NULL district gets repaired. The outlet CREATE path stores an
                        unresolvable geocoder hint as NULL rather than blocking a finished walk-in,
                        explicitly on the promise that it is fixed here; until then the outlet is
                        invisible to the district filter and to bulk assignment. The KEY is stored. */}
                    <span>{t('sales_agents:district', 'District')}:</span>
                    <Select style={{ minWidth: 240 }} value={outlet.district || undefined} allowClear showSearch optionFilterProp="label" placeholder="—" options={districtOptions} onChange={(v) => updateMutation.mutate({ id: outlet.id, payload: { district: v || null } })} data-testid="outlet-district-select" />
                  </Space>
                  {(outlet.dedupe_candidates || []).length > 0 && (
                    <Card size="small" title={t('sales_agents:dedupe_candidates', 'Possible duplicates found at creation')}>
                      {outlet.dedupe_candidates.map((c, i) => (
                        <div key={i}>{c.kind}: {c.name} {c.phone || ''} {c.distance_m != null ? `(${c.distance_m} m)` : ''} — {c.reason}</div>
                      ))}
                    </Card>
                  )}
                </Space>
              ),
            },
            {
              key: 'contacts',
              label: t('sales_agents:tab_contacts', 'Contacts'),
              children: <Table rowKey="id" pagination={false} dataSource={outlet.contacts || []} columns={[{ title: t('sales_agents:contact_name', 'Name'), dataIndex: 'name' }, { title: t('sales_agents:phone', 'Phone'), dataIndex: 'phone' }, { title: t('sales_agents:role', 'Role'), dataIndex: 'role' }]} />,
            },
            {
              key: 'visits',
              label: t('sales_agents:visits.tab_visits', 'Visits'),
              children: (
                <Space direction="vertical" size="small" style={{ width: '100%' }}>
                  {/* What the tab SHOWS, not what it asked for: one unpaged page of the same
                      route, capped at DEFAULT_PAGE_SIZE. "The last 90 days" alone was a claim
                      about the field that an outlet with forty visits quietly contradicted. */}
                  <Text type="secondary">{t('sales_agents:visits.drawer_window', 'The latest 20 visits in the last 90 days.')}</Text>
                  {/* A failed query clears `data` in react-query v5, so reading only `loading`
                      and `data` painted antd's empty state straight under that caption — an
                      assertion about the field built out of a request that never answered. The
                      house Alert, exactly as pages/Visits.js renders it. */}
                  {outletVisitsQuery.isError && (
                    <Alert
                      type="error"
                      showIcon
                      message={extractApiErrorMessage(outletVisitsQuery.error, t('ui.common.error_occurred', 'An error occurred'))}
                    />
                  )}
                  <Table
                    rowKey="id"
                    size="small"
                    pagination={false}
                    loading={outletVisitsQuery.isLoading}
                    dataSource={outletVisitsQuery.data?.visits || []}
                    columns={[
                      { title: t('sales_agents:when', 'When'), dataIndex: 'started_at', render: (v) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm') : '—') },
                      { title: t('sales_agents:agent', 'Agent'), dataIndex: 'agent_name', render: (v) => v || '—' },
                      { title: t('sales_agents:visits.columns.outcome', 'Outcome'), dataIndex: 'outcome', render: (v) => v || '—' },
                      { title: t('sales_agents:visits.columns.order', 'Order'), dataIndex: 'order_number', render: (v) => v || '—' },
                    ]}
                  />
                </Space>
              ),
            },
            {
              key: 'history',
              label: t('sales_agents:tab_history', 'History'),
              children: <Table rowKey="id" pagination={false} dataSource={detail?.stage_history || []} columns={[{ title: t('sales_agents:when', 'When'), dataIndex: 'created_at', render: (v) => (v ? v.replace('T', ' ').slice(0, 16) : '') }, { title: t('sales_agents:from', 'From'), dataIndex: 'from_stage', render: (v) => v || '—' }, { title: t('sales_agents:to', 'To'), dataIndex: 'to_stage' }, { title: t('sales_agents:reason', 'Reason'), dataIndex: 'reason_code', render: (v, r) => [v, r.note].filter(Boolean).join(' — ') || '—' }]} />,
            },
          ]} />
        ) : null}
      </Drawer>

      <Modal title={t('sales_agents:reject', 'Reject')} open={rejectOpen} onCancel={() => setRejectOpen(false)} footer={null} destroyOnClose>
        <Form form={rejectForm} layout="vertical" onFinish={(v) => rejectMutation.mutate({ id: outlet?.id, reason: v.reason })}>
          <Form.Item name="reason" label={t('sales_agents:reason', 'Reason')} rules={[{ required: true }]}><Input.TextArea rows={3} /></Form.Item>
          <Button type="primary" htmlType="submit" loading={rejectMutation.isPending}>{t('ui.common.save', 'Save')}</Button>
        </Form>
      </Modal>

      <Modal title={t('sales_agents:mark_lost', 'Mark lost')} open={lostOpen} onCancel={() => setLostOpen(false)} footer={null} destroyOnClose>
        <Form form={lostForm} layout="vertical" onFinish={(v) => lostMutation.mutate({ id: outlet?.id, reason: v.reason, note: v.note || null })}>
          <Form.Item name="reason" label={t('sales_agents:reason', 'Reason')} rules={[{ required: true }]}><Select options={LOST_REASONS.map((r) => ({ value: r, label: r }))} data-testid="lost-reason-select" /></Form.Item>
          <Form.Item name="note" label={t('sales_agents:notes', 'Notes')}><Input.TextArea rows={2} /></Form.Item>
          <Button type="primary" htmlType="submit" loading={lostMutation.isPending}>{t('ui.common.save', 'Save')}</Button>
        </Form>
      </Modal>

      <Modal title={t('sales_agents:bulk_assign', 'Bulk assign by district')} open={bulkOpen} onCancel={() => setBulkOpen(false)} footer={null} destroyOnClose>
        <Form form={bulkForm} layout="vertical" onFinish={(v) => bulkMutation.mutate({ district: v.district, agentUserId: v.agent_user_id })}>
          <Form.Item name="district" label={t('sales_agents:district', 'District')} rules={[{ required: true }]}>
            <Select showSearch optionFilterProp="label" options={districtOptions} data-testid="bulk-district-select" />
          </Form.Item>
          <Form.Item name="agent_user_id" label={t('sales_agents:agent', 'Agent')} rules={[{ required: true }]}>
            <Select options={agents.map((a) => ({ value: a.user_id, label: a.full_name }))} data-testid="bulk-agent-select" />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={bulkMutation.isPending}>{t('ui.common.save', 'Save')}</Button>
        </Form>
      </Modal>

      <Modal title={t('sales_agents:outlets.delivery_window.label', 'Delivery window')} open={windowOpen} onCancel={() => setWindowOpen(false)} footer={null} destroyOnClose>
        {/* destroyOnClose is what makes `initialValues` re-read the card on every open, so the
            modal always opens on what is actually stored rather than on the first outlet opened. */}
        {/* M28: antd here is 5.29.3, where `destroyOnClose` is DEPRECATED in favour of
            `destroyOnHidden` (renamed in 5.25) and logs a console warning. `destroyOnClose` is
            still what the two Modals directly above this one use (`Outlets.js:251`, `:258`), so
            this file stays consistent with itself — do not mix the two spellings in one page.
            Confirm the version before writing it: `grep -m1 '"version"' admin_ui/node_modules/antd/package.json`.
            If a vitest setup in this repo ever starts failing on console warnings, rename ALL
            THREE in this file together, not just the new one. */}
        <Form
          layout="vertical"
          initialValues={{
            delivery_window_start: outlet?.delivery_window_start ? dayjs(outlet.delivery_window_start, WINDOW_FORMAT) : null,
            delivery_window_end: outlet?.delivery_window_end ? dayjs(outlet.delivery_window_end, WINDOW_FORMAT) : null,
          }}
          onFinish={submitWindow}
        >
          <Text type="secondary">{t('sales_agents:outlets.delivery_window.help', 'The default delivery window for every order placed at this outlet. Clear both fields to remove it.')}</Text>
          <Row gutter={16} style={{ marginTop: 16 }}>
            <Col span={12}>
              <Form.Item name="delivery_window_start" label={t('sales_agents:outlets.delivery_window.start', 'From')}>
                <TimePicker format={WINDOW_FORMAT} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="delivery_window_end" label={t('sales_agents:outlets.delivery_window.end', 'Until')}>
                <TimePicker format={WINDOW_FORMAT} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          <Button type="primary" htmlType="submit" loading={updateMutation.isPending}>{t('ui.common.save', 'Save')}</Button>
        </Form>
      </Modal>
    </div>
  );
};

export default Outlets;
