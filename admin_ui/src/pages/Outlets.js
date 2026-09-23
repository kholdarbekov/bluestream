import React, { useMemo, useState } from 'react';
import {
  Alert, Card, Table, Tag, Space, Button, Input, Select, Row, Col, Statistic, Drawer, Descriptions, Typography,
  message, Modal, Form, Tabs, InputNumber, Popconfirm,
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
import OutletEditModal from '../components/sales/OutletEditModal';
import OutletContactsTab from '../components/sales/OutletContactsTab';
import OutletPhotosTab from '../components/sales/OutletPhotosTab';
import { OUTLET_CLASSES } from '../components/sales/outletVocabulary';
import dayjs from 'dayjs';

const { Title, Text } = Typography;

// Mirror business_app/models/sales.py — OUTLET_STAGES, OUTLET_TYPES, LOST_REASONS. OUTLET_CLASSES
// is shared with the Edit form, so it lives in components/sales/outletVocabulary.js.
const STAGES = ['prospect', 'trial', 'activation_requested', 'active', 'at_risk', 'dormant', 'lost'];
const STAGE_COLORS = {
  prospect: 'default', trial: 'purple', activation_requested: 'gold', active: 'green', at_risk: 'orange', dormant: 'blue', lost: 'red',
};
const STAGE_LABELS = {
  prospect: 'Prospect', trial: 'Trial', activation_requested: 'Awaiting activation', active: 'Active', at_risk: 'At risk', dormant: 'Dormant', lost: 'Lost',
};
const OUTLET_TYPES = ['grocery_store', 'workplace', 'individual'];
const LOST_REASONS = ['price', 'has_supplier', 'no_space', 'owner_absent', 'low_footfall', 'payment_terms', 'quality', 'closed', 'not_reached', 'other'];

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
  const [editOpen, setEditOpen] = useState(false);
  const [approveOpen, setApproveOpen] = useState(false);
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

  const approveMutation = useMutation({ mutationFn: ({ id, contractNumber, attach }) => salesService.approveOutlet(id, { contract_number: contractNumber, attach }), onSuccess: () => { message.success(t('sales_agents:outlet_approved', 'Outlet activated')); setApproveOpen(false); refresh(); }, onError });
  const rejectMutation = useMutation({ mutationFn: ({ id, reason }) => salesService.rejectOutlet(id, reason), onSuccess: () => { message.success(t('sales_agents:outlet_rejected', 'Request rejected')); setRejectOpen(false); rejectForm.resetFields(); refresh(); }, onError });
  const assignMutation = useMutation({ mutationFn: ({ id, agentUserId }) => salesService.assignOutlet(id, agentUserId), onSuccess: () => { message.success(t('sales_agents:outlet_assigned', 'Agent assigned')); refresh(); }, onError });
  const updateMutation = useMutation({ mutationFn: ({ id, payload }) => salesService.updateOutlet(id, payload), onSuccess: () => { message.success(t('sales_agents:outlet_updated', 'Outlet updated')); setEditOpen(false); refresh(); }, onError });
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
  // D25 branch mode, read ONCE from what the backend published. `is_branch` IS
  // `OutletService.is_branch` — the same answer the staff bot's card gates on — so neither
  // renderer re-derives the rule from `branch_count` (two derivations with two different null
  // defaults is exactly the duplication CLAUDE.md's "full scope" note forbids). `branch_count` is
  // only the NUMBER the account line prints.
  const isBranch = outlet?.is_branch === true;
  // Resolved server-side too: the outlet's primary phone already belongs to this account, so a
  // plain approve would 409. This page never matches a phone itself.
  const accountCandidate = outlet?.account_candidate || null;

  return (
    <div>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Col><Title level={3} style={{ margin: 0 }}>{t('sales_agents:outlets_title', 'Outlets')}</Title></Col>
        <Col>
          <Space>
            <Button icon={<TeamOutlined />} onClick={() => setBulkOpen(true)}>{t('sales_agents:bulk_assign', 'Bulk assign by district')}</Button>
            <Popconfirm title={t('sales_agents:import_confirm', 'Create an outlet for every grocery/workplace address that has none yet?')} onConfirm={() => importMutation.mutate()}>
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
            <Button onClick={() => setEditOpen(true)}>{t('sales_agents:outlets.edit', 'Edit')}</Button>
            {canApprove && (
              <Button type="primary" onClick={() => setApproveOpen(true)}>
                {accountCandidate
                  ? t('sales_agents:attach_modal_title', { account: accountCandidate.name, defaultValue: 'Attach to {{account}}' })
                  : t('sales_agents:approve', 'Approve')}
              </Button>
            )}
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
                  {/* D25: whose account this branch belongs to. Above the Descriptions rather
                      than inside it — the grid is `column={2}` and a ninth item would reflow
                      every pair below it. Gated on branch mode for the same reason the staff
                      card is: a single-outlet account has nothing to disambiguate. */}
                  {isBranch && (
                    <Text type="secondary">{t('sales_agents:outlet_account_line', { account: outlet.account_name || '—', count: outlet.branch_count, defaultValue: 'Account: {{account}} · {{count}} branches' })}</Text>
                  )}
                  <Descriptions bordered column={2} size="small">
                    <Descriptions.Item label={t('sales_agents:stage', 'Stage')}><Tag color={STAGE_COLORS[outlet.stage] || 'default'}>{outlet.stage}</Tag></Descriptions.Item>
                    <Descriptions.Item label={t('sales_agents:outlet_type', 'Type')}>{outlet.outlet_type}</Descriptions.Item>
                    <Descriptions.Item label={t('sales_agents:class', 'Class')}>{outlet.class || '—'}</Descriptions.Item>
                    <Descriptions.Item label={t('sales_agents:outlet_address', 'Address')}>{outlet.address_text || '—'}</Descriptions.Item>
                    {/* NULL means "not applicable", and `OutletService.card` is the only place that
                        decides it: no customer account means no wallet, no address row means no
                        bottle ledger. `?? 0` printed "Owes: 0 UZS" on a prospect that has never
                        ordered, which reads as a settled bill. A real 0 still renders as 0. */}
                    {/* D25: the figure is unchanged — the receivable is account-wide in every
                        mode and the bottle ledger is per address. In branch mode the labels say
                        which, because "Owes" on one branch of a chain reads as that branch's
                        debt. The money qualifier also reads `open_receivable_scope`, which is
                        what the backend says the figure MEANS: if the receivable ever stopped
                        being account-wide this label would stop claiming it is. */}
                    <Descriptions.Item label={isBranch && outlet.open_receivable_scope === 'account' ? t('sales_agents:open_receivable_account', 'Owes (account)') : t('sales_agents:open_receivable', 'Owes')}>{outlet.open_receivable == null ? '—' : `${outlet.open_receivable} UZS`}</Descriptions.Item>
                    <Descriptions.Item label={isBranch ? t('sales_agents:bottles_branch', 'Bottles at this branch') : t('sales_agents:bottle_balance', 'Bottles at outlet')}>{outlet.bottle_balance ?? '—'}</Descriptions.Item>
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
              children: <OutletContactsTab outlet={outlet} onChanged={refresh} />,
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
              key: 'photos',
              label: t('sales_agents:photos.tab', 'Photos'),
              // Mounted only while open, like the Visits tab's query: the drawer opens on Overview
              // and should not stream a gallery of photos from Telegram nobody asked to see.
              children: drawerTab === 'photos' ? <OutletPhotosTab outletId={outlet.id} /> : null,
            },
            {
              key: 'history',
              label: t('sales_agents:tab_history', 'History'),
              children: <Table rowKey="id" pagination={false} dataSource={detail?.stage_history || []} columns={[{ title: t('sales_agents:when', 'When'), dataIndex: 'created_at', render: (v) => (v ? v.replace('T', ' ').slice(0, 16) : '') }, { title: t('sales_agents:from', 'From'), dataIndex: 'from_stage', render: (v) => v || '—' }, { title: t('sales_agents:to', 'To'), dataIndex: 'to_stage' }, { title: t('sales_agents:reason', 'Reason'), dataIndex: 'reason_code', render: (v, r) => [v, r.note].filter(Boolean).join(' — ') || '—' }]} />,
            },
          ]} />
        ) : null}
      </Drawer>

      {/* D25: one button, two doors. With no `account_candidate` the backend creates a customer
          account, as it always has. With one, the outlet's primary phone already belongs to an
          account and a plain approve is refused (409 SALES_APPROVAL_PHONE_TAKEN) — so the modal
          names that account and sends `attach: true`, which joins the outlet to it as a branch.
          The choice is the backend's; this modal only labels it and offers it. */}
      <Modal
        title={accountCandidate
          ? t('sales_agents:attach_modal_title', { account: accountCandidate.name, defaultValue: 'Attach to {{account}}' })
          : t('sales_agents:approve_modal_title', 'Approve outlet')}
        open={approveOpen}
        onCancel={() => setApproveOpen(false)}
        footer={null}
        destroyOnClose
      >
        {/* No `form` instance on purpose, exactly like OutletEditModal below:
            destroyOnClose unmounts the fields, so the next open starts empty. */}
        <Form layout="vertical" onFinish={(v) => approveMutation.mutate({ id: outlet?.id, contractNumber: v.contract_number || null, attach: Boolean(accountCandidate) })}>
          {accountCandidate ? (
            /* R27 — attach mode is the sentence and Save, nothing else. A contract number typed
               here would be silently ignored whenever the account already has an active AMOUNT
               contract, which is the normal case for a chain; and an account without one is
               numbered by the backend exactly as a contract-less approve numbers it
               (`SA-<outlet_id>-<yyyymmdd>`). A field whose value usually vanishes is worse than
               no field, so the body carries `contract_number: null`. */
            <Text type="secondary">{t('sales_agents:attach_confirm', { account: accountCandidate.name, defaultValue: 'This outlet joins {{account}} as a branch — no new customer account is created.' })}</Text>
          ) : (
            /* Approve mode only. `ApprovePayload.contract_number` is max_length=100 and has been
               accepted by both approve doors since phase 1 with no way to fill it in. Optional:
               left empty, the backend numbers the contract itself. */
            <Form.Item name="contract_number" label={t('sales_agents:contract_number_label', 'Contract number')}>
              <Input maxLength={100} />
            </Form.Item>
          )}
          <Button type="primary" htmlType="submit" loading={approveMutation.isPending}>{t('ui.common.save', 'Save')}</Button>
        </Form>
      </Modal>

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

      <OutletEditModal
        outlet={outlet}
        open={editOpen}
        saving={updateMutation.isPending}
        onCancel={() => setEditOpen(false)}
        onSubmit={(payload) => updateMutation.mutate({ id: outlet.id, payload })}
      />
    </div>
  );
};

export default Outlets;
