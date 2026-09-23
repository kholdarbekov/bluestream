import React, { useMemo, useState } from 'react';
import {
  Alert, Button, Card, Col, DatePicker, Image, Row, Select, Space, Table, Tabs, Tag, Typography,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useQuery, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useLocation } from 'react-router-dom';
import dayjs from 'dayjs';
import salesService from '../services/salesService';
import staffService from '../services/staffService';
import OperationsMap from '../components/OperationsMap';
import VisitPhotoThumb from '../components/sales/VisitPhotoThumb';
import { fetchAllPages } from '../utils/pagination';
import { BULK_LOAD_PAGE_SIZE, DEFAULT_PAGE_SIZE } from '../utils/constants';
import { extractApiErrorMessage } from '../utils/apiError';

const { Title, Text } = Typography;
const { RangePicker } = DatePicker;

// Mirrors business_app/models/sales_visits.py:39 VISIT_OUTCOMES — the outcome filter's vocabulary.
// The EXCEPTION types are deliberately NOT mirrored: `GET /admin/sales/exceptions` publishes
// `types` with every page, so the feed's vocabulary stays one expression, on the server.
const VISIT_OUTCOMES = ['order_placed', 'no_order', 'closed', 'owner_absent', 'refused'];

// R12's default window, mirrored only so the picker OPENS on the seven days the backend would
// have defaulted to. The 92-day cap is NOT re-checked here — a refused range is the backend's
// answer (SALES_DATE_RANGE_INVALID) and is shown as its own message.
const DEFAULT_WINDOW_DAYS = 7;

// `_iso` publishes an offset-aware instant, so the browser renders it in the viewer's zone.
const stamp = (value) => (value ? dayjs(value).format('YYYY-MM-DD HH:mm') : '—');
const pct = (value) => (value == null ? '—' : `${value}%`);

// Rendered generically on purpose: a per-type branch here would be a second expression of the
// seven-type vocabulary the feed already owns (R8), and the type it gains next would render
// blank until someone noticed.
const renderDetail = (detail) => {
  const entries = Object.entries(detail || {});
  return entries.length
    ? entries.map(([key, value]) => `${key}: ${value == null ? '—' : value}`).join(' · ')
    : '—';
};

const Visits = () => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const queryClient = useQueryClient();
  const location = useLocation();

  // Deep link from the managers' 08:00 exception summary: /sales/visits?tab=exceptions (R9).
  // Read at mount so the feed is the FIRST thing fetched, instead of a visits list nobody
  // asked for followed by a tab flip.
  const [tab, setTab] = useState(() => (
    new URLSearchParams(location.search).get('tab') === 'exceptions' ? 'exceptions' : 'visits'
  ));
  const [range, setRange] = useState([dayjs().subtract(DEFAULT_WINDOW_DAYS - 1, 'day'), dayjs()]);
  const [agentId, setAgentId] = useState();
  const [outcome, setOutcome] = useState();
  const [inRadius, setInRadius] = useState();
  const [photoFilter, setPhotoFilter] = useState();
  const [exceptionType, setExceptionType] = useState();
  const [pagination, setPagination] = useState({ page: 1, per_page: DEFAULT_PAGE_SIZE });
  const [exceptionPagination, setExceptionPagination] = useState({ page: 1, per_page: DEFAULT_PAGE_SIZE });

  // Outlets.js:60 — a narrower filter usually has fewer pages than the one being viewed, which
  // renders an empty table beside a non-zero total. Both tables go back to page 1.
  const narrow = (setter) => (value) => {
    setter(value);
    setPagination((p) => ({ ...p, page: 1 }));
    setExceptionPagination((p) => ({ ...p, page: 1 }));
  };

  const period = useMemo(() => ({
    start_date: range[0].format('YYYY-MM-DD'),
    end_date: range[1].format('YYYY-MM-DD'),
    agent_id: agentId || undefined,
  }), [range, agentId]);

  const visitFilters = useMemo(() => ({
    ...period,
    outcome: outcome || undefined,
    // Tri-state. `undefined` is a THIRD answer ("either"), which is exactly what
    // business_app/utils/request_helpers.py::parse_bool_arg reads on the other side — a
    // `|| undefined` here would erase the deliberate `false`.
    in_radius: inRadius,
    photo: photoFilter || undefined,
    page: pagination.page,
    per_page: pagination.per_page,
  }), [period, outcome, inRadius, photoFilter, pagination]);

  const exceptionFilters = useMemo(() => ({
    ...period,
    type: exceptionType || undefined,
    page: exceptionPagination.page,
    per_page: exceptionPagination.per_page,
  }), [period, exceptionType, exceptionPagination]);

  const visitsQuery = useQuery({
    queryKey: ['visits', visitFilters],
    queryFn: () => salesService.getVisits(visitFilters),
    placeholderData: keepPreviousData,
    enabled: tab === 'visits',
  });

  const planQuery = useQuery({
    queryKey: ['salesPlanVsFact', period],
    queryFn: () => salesService.getPlanVsFact(period),
    placeholderData: keepPreviousData,
    enabled: tab === 'visits',
  });

  const exceptionsQuery = useQuery({
    queryKey: ['salesExceptions', exceptionFilters],
    queryFn: () => salesService.getExceptions(exceptionFilters),
    placeholderData: keepPreviousData,
    enabled: tab === 'exceptions',
  });

  // Same query key as Outlets.js:87 so react-query serves both pages from one fetch.
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

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['visits'] });
    queryClient.invalidateQueries({ queryKey: ['salesPlanVsFact'] });
    queryClient.invalidateQueries({ queryKey: ['salesExceptions'] });
  };

  const visits = useMemo(() => visitsQuery.data?.visits || [], [visitsQuery.data]);
  const planRows = planQuery.data?.rows || [];
  const exceptions = exceptionsQuery.data?.exceptions || [];
  const exceptionTypes = exceptionsQuery.data?.types || [];

  // The band draws the check-ins of THIS page only (R11). A visit with no coordinates — a
  // skipped check-in, or an outlet that has no pin yet — has nothing to draw and stays in the
  // table alone.
  const checkins = useMemo(() => visits
    .filter((v) => v.checkin_latitude != null && v.checkin_longitude != null)
    .map((v) => ({
      visit_id: v.id,
      lat: v.checkin_latitude,
      lng: v.checkin_longitude,
      in_radius: v.in_radius,
      distance_m: v.distance_m,
      outlet_name: v.outlet_name,
      agent_name: v.agent_name,
    })), [visits]);

  const renderCheckin = (record) => {
    if (record.checkin_skipped) return <Tag color="orange">{t('sales_agents:visits.checkin_skipped', 'Skipped')}</Tag>;
    // NULL is "not measurable", never a violation: no coordinates, or an outlet with no pin.
    if (record.in_radius == null) return <Tag>{t('sales_agents:visits.checkin_unmeasured', 'Not measured')}</Tag>;
    const metres = record.distance_m == null ? '' : ` (${Math.round(record.distance_m)} m)`;
    return record.in_radius
      ? <Tag color="green">{`${t('sales_agents:visits.filters.in_radius_true', 'In radius')}${metres}`}</Tag>
      : <Tag color="red">{`${t('sales_agents:visits.filters.in_radius_false', 'Out of range')}${metres}`}</Tag>;
  };

  const visitColumns = [
    { title: t('sales_agents:when', 'When'), dataIndex: 'started_at', key: 'started_at', render: stamp },
    { title: t('sales_agents:agent', 'Agent'), dataIndex: 'agent_name', key: 'agent_name', render: (v) => v || '—' },
    { title: t('sales_agents:outlet_name', 'Outlet'), dataIndex: 'outlet_name', key: 'outlet_name', render: (v) => v || '—' },
    {
      title: t('sales_agents:visits.columns.planned', 'Planned'),
      dataIndex: 'planned',
      key: 'planned',
      render: (value) => (value
        ? <Tag color="blue">{t('sales_agents:visits.planned_yes', 'Planned')}</Tag>
        : <Tag>{t('sales_agents:visits.planned_no', 'Unplanned')}</Tag>),
    },
    { title: t('sales_agents:visits.columns.outcome', 'Outcome'), dataIndex: 'outcome', key: 'outcome', render: (v) => v || '—' },
    { title: t('sales_agents:visits.columns.checkin', 'Check-in'), key: 'checkin', render: (_, record) => renderCheckin(record) },
    { title: t('sales_agents:visits.columns.order', 'Order'), dataIndex: 'order_number', key: 'order_number', render: (v) => v || '—' },
    {
      title: t('sales_agents:visits.columns.photos', 'Photos'),
      key: 'photos',
      // `photo_requested` is the backend's answer (D28): the page never decides which outlets
      // were asked for a photo.
      render: (_, record) => {
        if (record.photo_count > 0) return `📷 ${record.photo_count}`;
        return record.photo_requested
          ? <Tag color="orange">{t('sales_agents:visits.filters.photo_missing', 'No photo')}</Tag>
          : '—';
      },
    },
  ];

  const planColumns = [
    { title: t('sales_agents:visits.plan_vs_fact.day', 'Day'), dataIndex: 'day', key: 'day' },
    { title: t('sales_agents:agent', 'Agent'), dataIndex: 'agent_name', key: 'agent_name' },
    {
      title: t('sales_agents:visits.plan_vs_fact.due', 'Due'),
      dataIndex: 'due',
      key: 'due',
      // `due: null` is not a zero: it means the nightly 01:20 snapshot has no row for that day
      // (R1), which is why the backend ships `plan_source` instead of leaving the table to guess.
      render: (value, record) => (record.plan_source === 'none'
        ? <Tag>{t('sales_agents:visits.plan_vs_fact.no_plan', 'No plan')}</Tag>
        : value),
    },
    { title: t('sales_agents:visits.plan_vs_fact.completed', 'Completed'), dataIndex: 'completed', key: 'completed' },
    { title: t('sales_agents:visits.plan_vs_fact.unplanned', 'Unplanned'), dataIndex: 'unplanned', key: 'unplanned' },
    { title: t('sales_agents:visits.plan_vs_fact.strike_rate', 'Strike rate'), dataIndex: 'strike_rate_pct', key: 'strike_rate_pct', render: pct },
  ];

  const exceptionColumns = [
    { title: t('sales_agents:when', 'When'), dataIndex: 'occurred_at', key: 'occurred_at', render: stamp },
    {
      title: t('sales_agents:visits.filters.type', 'Exception type'),
      dataIndex: 'type',
      key: 'type',
      render: (value) => <Tag>{t(`sales_agents:exceptions.type.${value}`, value)}</Tag>,
    },
    { title: t('sales_agents:agent', 'Agent'), dataIndex: 'agent_name', key: 'agent_name', render: (v) => v || '—' },
    { title: t('sales_agents:outlet_name', 'Outlet'), dataIndex: 'outlet_name', key: 'outlet_name', render: (v) => v || '—' },
    { title: t('sales_agents:visits.columns.detail', 'Detail'), dataIndex: 'detail', key: 'detail', render: renderDetail },
  ];

  const errorAlert = (query) => (query.isError ? (
    <Alert
      type="error"
      showIcon
      message={extractApiErrorMessage(query.error, t('ui.common.error_occurred', 'An error occurred'))}
    />
  ) : null);

  const tabItems = [
    {
      key: 'visits',
      label: t('sales_agents:visits.tab_visits', 'Visits'),
      children: (
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
          {errorAlert(visitsQuery)}
          {/* Its own alert: the two tables on this tab are two independent queries, and a
              plan-vs-fact that failed alone would otherwise render as an empty plan table beside
              a full visits table — which reads as "nobody had a plan", a claim about the field
              rather than about the request that was refused. */}
          {errorAlert(planQuery)}
          <Card title={t('sales_agents:visits.plan_vs_fact.title', 'Plan vs fact')} data-testid="plan-vs-fact">
            <Table
              columns={planColumns}
              dataSource={planRows}
              rowKey={(r) => `${r.agent_user_id}-${r.day}`}
              loading={planQuery.isLoading}
              pagination={false}
              size="small"
              scroll={{ x: 720 }}
            />
          </Card>
          <Card>
            <Text type="secondary">{t('sales_agents:visits.map_caption', 'The map shows the check-ins on this page of the table only.')}</Text>
            <OperationsMap
              height={360}
              checkins={checkins}
              visibleLayers={{ customers: false, orders: false, drivers: false, checkins: true }}
            />
          </Card>
          <Card data-testid="visits-table">
            <Table
              columns={visitColumns}
              dataSource={visits}
              rowKey="id"
              loading={visitsQuery.isLoading}
              pagination={{
                current: pagination.page,
                pageSize: pagination.per_page,
                total: visitsQuery.data?.meta?.total || 0,
                onChange: (page, per_page) => setPagination({ page, per_page }),
                // The band draws THIS page's check-ins (R11), so the page size is how much
                // of the period reaches the map. 100 is the route's own per_page cap: a
                // supervisor who wants the week on one map picks it, and nobody can ask
                // for a page the backend would refuse.
                showSizeChanger: true,
                pageSizeOptions: [20, 50, 100],
              }}
              expandable={{
                rowExpandable: (record) => record.photo_count > 0,
                expandedRowRender: (record) => (
                  <Image.PreviewGroup>
                    <Space wrap>{record.photos.map((photo) => <VisitPhotoThumb key={photo.id} photo={photo} />)}</Space>
                  </Image.PreviewGroup>
                ),
              }}
              scroll={{ x: 900 }}
            />
          </Card>
        </Space>
      ),
    },
    {
      key: 'exceptions',
      label: t('sales_agents:visits.tab_exceptions', 'Exceptions'),
      children: (
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
          {errorAlert(exceptionsQuery)}
          <Card>
            <Space wrap>
              <Select
                placeholder={t('sales_agents:visits.filters.type', 'Exception type')}
                value={exceptionType}
                onChange={narrow(setExceptionType)}
                allowClear
                style={{ minWidth: 240 }}
                options={exceptionTypes.map((v) => ({ value: v, label: t(`sales_agents:exceptions.type.${v}`, v) }))}
                data-testid="filter-exception-type"
              />
              {/* Two of the seven types are answered AS OF NOW and ignore the period (R8) —
                  said out loud, because read as "in this period" they would look like a bug. */}
              <Text type="secondary">{t('sales_agents:visits.exceptions_caption', 'Unvisited outlets and duplicate open try-outs are as of now — the period does not narrow them.')}</Text>
            </Space>
          </Card>
          <Card data-testid="exceptions-table">
            <Table
              columns={exceptionColumns}
              dataSource={exceptions}
              // The feed row has no id of its own: it is a superset row assembled from seven
              // queries, so the key is the tuple that identifies the event PLUS the row's index.
              // The index is not decoration: `duplicate_photo` emits one row per photo, so two
              // rows legitimately share type, outlet, visit and `occurred_at` (`received_at`
              // carries no per-row uniqueness) and the tuple alone is the same string twice.
              // Safe as a discriminator because the server orders the page deterministically
              // (`occurred_at` desc) and this table never re-sorts or filters client-side.
              rowKey={(r, index) => `${r.type}-${r.occurred_at}-${r.outlet_id}-${r.visit_id}-${index}`}
              loading={exceptionsQuery.isLoading}
              pagination={{
                current: exceptionPagination.page,
                pageSize: exceptionPagination.per_page,
                total: exceptionsQuery.data?.meta?.total || 0,
                onChange: (page, per_page) => setExceptionPagination({ page, per_page }),
                showSizeChanger: false,
              }}
              scroll={{ x: 900 }}
            />
          </Card>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Col><Title level={3} style={{ margin: 0 }}>{t('sales_agents:visits.title', 'Visits')}</Title></Col>
        <Col><Button icon={<ReloadOutlined />} onClick={refresh}>{t('ui.common.refresh', 'Refresh')}</Button></Col>
      </Row>

      <Card style={{ marginBottom: 16 }}>
        <Row gutter={[16, 16]} align="middle">
          <Col xs={24} sm={8}>
            <RangePicker
              value={range}
              allowClear={false}
              format="YYYY-MM-DD"
              style={{ width: '100%' }}
              onChange={(value) => value && value[0] && value[1] && narrow(setRange)(value)}
            />
          </Col>
          <Col xs={12} sm={4}>
            <Select
              placeholder={t('sales_agents:agent', 'Agent')}
              value={agentId}
              onChange={narrow(setAgentId)}
              allowClear
              style={{ width: '100%' }}
              options={agents.map((a) => ({ value: a.user_id, label: a.full_name }))}
              data-testid="filter-agent"
            />
          </Col>
          {/* Outcome and check-in narrow VISITS and nothing else — `GET /admin/sales/exceptions`
              accepts neither. On the feed they would be controls that change no result and
              silently send it back to page 1 through `narrow`. Period and agent narrow both, so
              they stay on both tabs. */}
          {tab === 'visits' && (
          <Col xs={12} sm={4}>
            <Select
              placeholder={t('sales_agents:visits.columns.outcome', 'Outcome')}
              value={outcome}
              onChange={narrow(setOutcome)}
              allowClear
              style={{ width: '100%' }}
              options={VISIT_OUTCOMES.map((v) => ({ value: v, label: v }))}
              data-testid="filter-outcome"
            />
          </Col>
          )}
          {tab === 'visits' && (
          <Col xs={12} sm={4}>
            <Select
              placeholder={t('sales_agents:visits.columns.checkin', 'Check-in')}
              value={inRadius}
              onChange={narrow(setInRadius)}
              allowClear
              style={{ width: '100%' }}
              options={[
                { value: true, label: t('sales_agents:visits.filters.in_radius_true', 'In radius') },
                { value: false, label: t('sales_agents:visits.filters.in_radius_false', 'Out of range') },
              ]}
              data-testid="filter-in-radius"
            />
          </Col>
          )}
          {tab === 'visits' && (
          <Col xs={12} sm={4}>
            <Select
              placeholder={t('sales_agents:visits.filters.photo', 'Photo')}
              value={photoFilter}
              onChange={narrow(setPhotoFilter)}
              allowClear
              style={{ width: '100%' }}
              options={[{ value: 'missing', label: t('sales_agents:visits.filters.photo_missing', 'No photo') }]}
              data-testid="filter-photo"
            />
          </Col>
          )}
        </Row>
      </Card>

      <Tabs activeKey={tab} onChange={setTab} items={tabItems} />
    </div>
  );
};

export default Visits;
