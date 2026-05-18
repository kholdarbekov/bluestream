import React, { useState, useMemo } from 'react';
import {
  Tabs,
  Card,
  Form,
  Input,
  InputNumber,
  Select,
  Switch,
  Slider,
  Button,
  Space,
  Table,
  Tag,
  message,
  Modal,
  Drawer,
  Typography,
  Row,
  Col,
  Statistic,
  Tooltip,
  TimePicker,
} from 'antd';
import {
  ReloadOutlined,
  PlayCircleOutlined,
  SaveOutlined,
  ThunderboltOutlined,
  EditOutlined,
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';
import adminService from '../services/adminService';

const { Option } = Select;
const { Text, Title } = Typography;

const STATUS_COLORS = {
  running: 'processing',
  success: 'success',
  failed: 'error',
  skipped: 'default',
};

// Lookup table is a closed allowlist; map indirection avoids the
// detect-object-injection sink while keeping the call site terse.
const STATUS_COLOR_MAP = new Map(Object.entries(STATUS_COLORS));
const statusColor = (v) => STATUS_COLOR_MAP.get(v) || 'default';

const RUN_KIND_OPTIONS = ['daily', 'manual', 'low_water', 'on_empty'];

const SCHEDULE_TYPE_OPTIONS = [
  { value: 'daily', labelKey: 'ui.marking_codes.schedule.daily' },
  { value: 'weekly', labelKey: 'ui.marking_codes.schedule.weekly' },
  { value: 'interval_days', labelKey: 'ui.marking_codes.schedule.interval' },
];

const DAY_OF_WEEK_LABELS = [
  'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'
];


// ---------- Schedule & Config tab ----------------------------------------

function ScheduleConfigTab() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [form] = Form.useForm();
  const scheduleType = Form.useWatch('schedule_type', form);

  const { data: configResp, isLoading } = useQuery({
    queryKey: ['marking-code-config'],
    queryFn: () => adminService.getMarkingCodeTaskConfig(),
  });

  const global = configResp?.data?.global;

  React.useEffect(() => {
    if (!global) return;
    form.setFieldsValue({
      schedule_type: global.schedule_type,
      interval_days: global.interval_days,
      day_of_week: global.day_of_week,
      execution_time: dayjs()
        .hour(global.execution_hour || 0)
        .minute(global.execution_minute || 0)
        .second(0),
      target_min: global.target_min,
      target_max: global.target_max,
      trend_window_days: global.trend_window_days,
      runway_days: global.runway_days,
      safety_multiplier: global.safety_multiplier,
      low_water_ratio: global.low_water_ratio,
      asl_belgisi_utilisation_api_chunk_size: global.asl_belgisi_utilisation_api_chunk_size,
      tc_utilisation_enabled: global.tc_utilisation_enabled,
      tc_utilisation_delay_seconds: global.tc_utilisation_delay_seconds,
    });
  }, [global, form]);

  const saveMutation = useMutation({
    mutationFn: (payload) => adminService.updateMarkingCodeTaskConfig(payload),
    onSuccess: () => {
      message.success(t('ui.marking_codes.actions.save_success', 'Saved. The beat container will reload within ~1 minute.'));
      queryClient.invalidateQueries({ queryKey: ['marking-code-config'] });
    },
    onError: (err) => {
      const detail = err?.response?.data?.message || err?.message || 'Save failed';
      message.error(detail);
    },
  });

  const onFinish = (values) => {
    const time = values.execution_time || dayjs();
    const payload = {
      schedule_type: values.schedule_type,
      execution_hour: time.hour(),
      execution_minute: time.minute(),
      target_min: values.target_min,
      target_max: values.target_max,
      trend_window_days: values.trend_window_days,
      runway_days: values.runway_days,
      safety_multiplier: values.safety_multiplier,
      low_water_ratio: values.low_water_ratio,
      asl_belgisi_utilisation_api_chunk_size: values.asl_belgisi_utilisation_api_chunk_size,
      tc_utilisation_enabled: values.tc_utilisation_enabled,
      tc_utilisation_delay_seconds: values.tc_utilisation_delay_seconds,
    };
    if (values.schedule_type === 'weekly') {
      payload.day_of_week = values.day_of_week;
    } else {
      payload.day_of_week = null;
    }
    if (values.schedule_type === 'interval_days') {
      payload.interval_days = values.interval_days;
    } else {
      payload.interval_days = null;
    }
    saveMutation.mutate(payload);
  };

  return (
    <Form
      form={form}
      layout="vertical"
      onFinish={onFinish}
      disabled={isLoading}
    >
      <Row gutter={16}>
        <Col xs={24} lg={12}>
          <Card title={t('ui.marking_codes.section.schedule', 'Schedule')} size="small">
            <Form.Item
              name="schedule_type"
              label={t('ui.marking_codes.fields.schedule_type', 'Frequency')}
              rules={[{ required: true }]}
            >
              <Select>
                {SCHEDULE_TYPE_OPTIONS.map((opt) => (
                  <Option key={opt.value} value={opt.value}>
                    {t(opt.labelKey, opt.value)}
                  </Option>
                ))}
              </Select>
            </Form.Item>

            {scheduleType === 'weekly' && (
              <Form.Item
                name="day_of_week"
                label={t('ui.marking_codes.fields.day_of_week', 'Day of week')}
                rules={[{ required: true, message: 'Required for weekly' }]}
              >
                <Select>
                  {DAY_OF_WEEK_LABELS.map((d, i) => (
                    <Option key={i} value={i}>{d}</Option>
                  ))}
                </Select>
              </Form.Item>
            )}

            {scheduleType === 'interval_days' && (
              <Form.Item
                name="interval_days"
                label={t('ui.marking_codes.fields.interval_days', 'Run every N days')}
                rules={[{ required: true, message: 'Required for interval' }]}
              >
                <InputNumber min={1} max={30} />
              </Form.Item>
            )}

            <Form.Item
              name="execution_time"
              label={t('ui.marking_codes.fields.execution_time', 'Execution time (UTC)')}
              rules={[{ required: true }]}
            >
              <TimePicker format="HH:mm" minuteStep={5} />
            </Form.Item>
          </Card>
        </Col>

        <Col xs={24} lg={12}>
          <Card
            title={t('ui.marking_codes.section.target_sizing', 'Quantity to utilise (sales-trend formula)')}
            size="small"
            extra={
              <Tooltip
                title={t(
                  'ui.marking_codes.section.target_sizing_help',
                  'The number of codes utilised per run is computed from recent sales: ceil(avg_daily_qty * runway_days * safety_multiplier), clamped to [target_min, target_max].',
                )}
              >
                <span style={{ cursor: 'help', color: '#999' }}>ⓘ</span>
              </Tooltip>
            }
          >
            <Row gutter={16}>
              <Col span={12}>
                <Form.Item
                  name="target_min"
                  label="target_min"
                  tooltip="Lower bound for the computed target (cold-start floor)."
                  rules={[{ required: true }]}
                >
                  <InputNumber min={1} max={10000} style={{ width: '100%' }} />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item
                  name="target_max"
                  label="target_max"
                  tooltip="Upper bound for the computed target (runaway cap)."
                  rules={[{ required: true }]}
                >
                  <InputNumber min={1} max={10000} style={{ width: '100%' }} />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item
                  name="trend_window_days"
                  label="trend_window_days"
                  tooltip="How many past days of card/click sales feed the avg_daily calculation."
                  rules={[{ required: true }]}
                >
                  <InputNumber min={1} max={90} style={{ width: '100%' }} />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item
                  name="runway_days"
                  label="runway_days"
                  tooltip="How many days of demand the pool should cover after each run."
                  rules={[{ required: true }]}
                >
                  <InputNumber min={1} max={30} style={{ width: '100%' }} />
                </Form.Item>
              </Col>
              <Col span={24}>
                <Form.Item
                  name="safety_multiplier"
                  label="safety_multiplier"
                  tooltip="Multiplier on the avg_daily target to absorb traffic spikes."
                  rules={[{ required: true }]}
                >
                  <InputNumber min={0.5} max={5.0} step={0.1} style={{ width: '100%' }} />
                </Form.Item>
              </Col>
            </Row>
          </Card>
        </Col>

        <Col xs={24} lg={12}>
          <Card
            title={t('ui.marking_codes.section.thresholds', 'Pool thresholds & API transport')}
            size="small"
            style={{ marginTop: 16 }}
          >
            <Form.Item
              name="low_water_ratio"
              label="low_water_ratio (0–1)"
              tooltip="When pre_utilised drops below target × ratio, an intra-day replenish fires."
              rules={[{ required: true }]}
            >
              <Slider min={0.05} max={1.0} step={0.05}
                marks={{ 0.05: '0.05', 0.25: '0.25', 0.5: '0.5', 1.0: '1.0' }} />
            </Form.Item>
            <Form.Item
              name="asl_belgisi_utilisation_api_chunk_size"
              label="asl_belgisi_utilisation_api_chunk_size"
              tooltip={t(
                'ui.marking_codes.fields.api_chunk_size_help',
                'HTTP chunk size for the Asl Belgisi /utilisation API. Does NOT control how many codes to utilise — that comes from the sales-trend formula. Only splits the deficit into smaller requests (e.g. deficit=450, chunk=200 → 3 calls of 200+200+50).',
              )}
              rules={[{ required: true }]}
            >
              <InputNumber min={1} max={1000} style={{ width: '100%' }} />
            </Form.Item>
          </Card>
        </Col>

        <Col xs={24} lg={12}>
          <Card
            title={t('ui.marking_codes.section.tc_behavior', 'Tax Committee behavior')}
            size="small"
            style={{ marginTop: 16 }}
          >
            <Form.Item
              name="tc_utilisation_enabled"
              label="tc_utilisation_enabled"
              valuePropName="checked"
            >
              <Switch />
            </Form.Item>
            <Form.Item name="tc_utilisation_delay_seconds" label="tc_utilisation_delay_seconds">
              <InputNumber min={0} max={3600} style={{ width: '100%' }} />
            </Form.Item>
          </Card>
        </Col>
      </Row>

      <div style={{ marginTop: 16, textAlign: 'right' }}>
        <Button
          type="primary"
          htmlType="submit"
          icon={<SaveOutlined />}
          loading={saveMutation.isPending}
        >
          {t('ui.marking_codes.actions.save', 'Save')}
        </Button>
      </div>
    </Form>
  );
}


// ---------- Task Runs tab ------------------------------------------------

function TaskRunsTab() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [filters, setFilters] = useState({});
  const [pagination, setPagination] = useState({ page: 1, per_page: 25 });
  const [selectedRun, setSelectedRun] = useState(null);

  const { data: stats } = useQuery({
    queryKey: ['marking-code-stats', 7],
    queryFn: () => adminService.getMarkingCodeTaskStats(7),
    refetchInterval: 15000,
  });

  const { data: runsResp, isLoading, refetch } = useQuery({
    queryKey: ['marking-code-runs', filters, pagination],
    queryFn: () => adminService.listMarkingCodeTaskRuns({
      ...filters,
      page: pagination.page,
      per_page: pagination.per_page,
    }),
    placeholderData: keepPreviousData,
    refetchInterval: 10000,
  });

  const triggerMutation = useMutation({
    mutationFn: (payload) => adminService.triggerMarkingCodeTaskRun(payload),
    onSuccess: () => {
      message.success(t('ui.marking_codes.actions.run_enqueued', 'Run enqueued'));
      queryClient.invalidateQueries({ queryKey: ['marking-code-runs'] });
    },
    onError: (err) => {
      message.error(err?.response?.data?.message || 'Failed to enqueue run');
    },
  });

  const runs = runsResp?.data?.items || [];
  const total = runsResp?.meta?.total || 0;

  const statsData = stats?.data || {};

  const columns = [
    {
      title: 'Started',
      dataIndex: 'started_at',
      width: 170,
      render: (v) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm:ss') : '—'),
    },
    {
      title: 'Task',
      dataIndex: 'task_name',
      render: (v, row) => (
        <Space size={4}>
          <Tag color={row.parent_run_id ? 'blue' : 'purple'}>
            {row.parent_run_id ? 'child' : 'parent'}
          </Tag>
          <Text style={{ fontSize: 12 }}>{v}</Text>
        </Space>
      ),
    },
    {
      title: 'Product',
      dataIndex: 'product_name',
      render: (v, row) => v || (row.product_id ? `#${row.product_id}` : '—'),
    },
    {
      title: 'Run kind',
      dataIndex: 'run_kind',
      width: 100,
    },
    {
      title: 'Status',
      dataIndex: 'status',
      width: 110,
      render: (v) => <Tag color={statusColor(v)}>{v}</Tag>,
    },
    {
      title: 'Duration',
      dataIndex: 'duration_ms',
      width: 100,
      render: (v) => (v != null ? `${v} ms` : '—'),
    },
    {
      title: 'Utilised',
      dataIndex: 'utilised',
      width: 90,
    },
    {
      title: 'Errors',
      dataIndex: 'errors',
      width: 80,
      render: (v) => (v ? <Tag color="error">{v}</Tag> : v),
    },
  ];

  return (
    <>
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={24} sm={6}>
          <Card><Statistic title="Runs (7d)" value={statsData.total_runs ?? 0} /></Card>
        </Col>
        <Col xs={24} sm={6}>
          <Card>
            <Statistic
              title="Success rate (7d)"
              value={statsData.success_rate != null ? (statsData.success_rate * 100).toFixed(1) : '—'}
              suffix={statsData.success_rate != null ? '%' : ''}
            />
          </Card>
        </Col>
        <Col xs={24} sm={6}>
          <Card><Statistic title="Codes utilised (7d)" value={statsData.totals?.utilised ?? 0} /></Card>
        </Col>
        <Col xs={24} sm={6}>
          <Card><Statistic title="Errors (7d)" value={statsData.totals?.errors ?? 0} /></Card>
        </Col>
      </Row>

      <Card
        size="small"
        style={{ marginBottom: 16 }}
        title="Filters"
        extra={
          <Space>
            <Button
              icon={<ReloadOutlined />}
              onClick={() => refetch()}
            >
              Refresh
            </Button>
            <Button
              type="primary"
              icon={<ThunderboltOutlined />}
              loading={triggerMutation.isPending}
              onClick={() => triggerMutation.mutate({ scope: 'all' })}
            >
              {t('ui.marking_codes.actions.run_all', 'Run for all products')}
            </Button>
          </Space>
        }
      >
        <Space wrap>
          <Select
            allowClear
            placeholder="Status"
            style={{ minWidth: 140 }}
            value={filters.status}
            onChange={(v) => setFilters((f) => ({ ...f, status: v }))}
          >
            {Object.keys(STATUS_COLORS).map((s) => <Option key={s} value={s}>{s}</Option>)}
          </Select>
          <Select
            allowClear
            placeholder="Run kind"
            style={{ minWidth: 140 }}
            value={filters.run_kind}
            onChange={(v) => setFilters((f) => ({ ...f, run_kind: v }))}
          >
            {RUN_KIND_OPTIONS.map((k) => <Option key={k} value={k}>{k}</Option>)}
          </Select>
          <Select
            allowClear
            placeholder="Task"
            style={{ minWidth: 240 }}
            value={filters.task_name}
            onChange={(v) => setFilters((f) => ({ ...f, task_name: v }))}
          >
            <Option value="pre_register_marking_codes_daily">pre_register_marking_codes_daily</Option>
            <Option value="replenish_marking_codes_for_product">replenish_marking_codes_for_product</Option>
          </Select>
          <InputNumber
            placeholder="Product ID"
            value={filters.product_id}
            onChange={(v) => setFilters((f) => ({ ...f, product_id: v || undefined }))}
          />
        </Space>
      </Card>

      <Table
        size="small"
        rowKey="id"
        columns={columns}
        dataSource={runs}
        loading={isLoading}
        onRow={(row) => ({
          onClick: () => setSelectedRun(row),
          style: { cursor: 'pointer' },
        })}
        pagination={{
          current: pagination.page,
          pageSize: pagination.per_page,
          total,
          showSizeChanger: true,
          onChange: (page, per_page) => setPagination({ page, per_page }),
        }}
      />

      <Drawer
        title={selectedRun ? `Run #${selectedRun.id}` : ''}
        width={720}
        open={!!selectedRun}
        onClose={() => setSelectedRun(null)}
      >
        {selectedRun && <RunDetail runId={selectedRun.id} />}
      </Drawer>
    </>
  );
}


function RunDetail({ runId }) {
  const { data, isLoading } = useQuery({
    queryKey: ['marking-code-run', runId],
    queryFn: () => adminService.getMarkingCodeTaskRun(runId),
    enabled: !!runId,
  });

  if (isLoading) return <Text>Loading…</Text>;
  const run = data?.data?.run;
  if (!run) return <Text>Run not found.</Text>;

  const children = run.children || [];

  return (
    <>
      <Title level={5}>Summary</Title>
      <pre style={{ background: '#fafafa', padding: 12, borderRadius: 4, fontSize: 12 }}>
        {JSON.stringify(run.result_summary || {}, null, 2)}
      </pre>
      {run.error_message && (
        <>
          <Title level={5}>Error</Title>
          <pre style={{ color: '#cf1322', fontSize: 12 }}>{run.error_message}</pre>
        </>
      )}
      {children.length > 0 && (
        <>
          <Title level={5}>Child runs ({children.length})</Title>
          <Table
            size="small"
            rowKey="id"
            pagination={false}
            dataSource={children}
            columns={[
              { title: 'Product', dataIndex: 'product_name', render: (v, r) => v || `#${r.product_id}` },
              { title: 'Status', dataIndex: 'status', render: (v) => <Tag color={statusColor(v)}>{v}</Tag> },
              { title: 'Utilised', dataIndex: 'utilised' },
              { title: 'Skipped', dataIndex: 'skipped_invalid' },
              { title: 'Errors', dataIndex: 'errors' },
              { title: 'Duration', dataIndex: 'duration_ms', render: (v) => (v != null ? `${v} ms` : '—') },
            ]}
          />
        </>
      )}
    </>
  );
}


// ---------- Pool Status tab ----------------------------------------------

function PoolStatusTab() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [editTarget, setEditTarget] = useState(null);
  const [overrideForm] = Form.useForm();

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['marking-code-pool-status'],
    queryFn: () => adminService.getMarkingCodePoolStatus(),
    refetchInterval: 30000,
  });

  const triggerMutation = useMutation({
    mutationFn: (productId) =>
      adminService.triggerMarkingCodeTaskRun({ scope: 'product', product_id: productId }),
    onSuccess: () => {
      message.success(t('ui.marking_codes.actions.run_enqueued', 'Run enqueued'));
      queryClient.invalidateQueries({ queryKey: ['marking-code-runs'] });
    },
    onError: (err) => message.error(err?.response?.data?.message || 'Failed to enqueue run'),
  });

  const overrideMutation = useMutation({
    mutationFn: ({ productId, payload }) =>
      adminService.updateProductMarkingCodeOverrides(productId, payload),
    onSuccess: () => {
      message.success(t('ui.marking_codes.actions.overrides_saved', 'Overrides saved'));
      setEditTarget(null);
      queryClient.invalidateQueries({ queryKey: ['marking-code-pool-status'] });
    },
    onError: (err) => message.error(err?.response?.data?.message || 'Failed to save overrides'),
  });

  const items = data?.data?.items || [];

  const openEdit = (row) => {
    overrideForm.setFieldsValue({
      target_min: row.overrides.target_min ?? undefined,
      target_max: row.overrides.target_max ?? undefined,
      trend_window_days: row.overrides.trend_window_days ?? undefined,
      runway_days: row.overrides.runway_days ?? undefined,
      safety_multiplier: row.overrides.safety_multiplier ?? undefined,
      low_water_ratio: row.overrides.low_water_ratio ?? undefined,
      asl_belgisi_utilisation_api_chunk_size:
        row.overrides.asl_belgisi_utilisation_api_chunk_size ?? undefined,
    });
    setEditTarget(row);
  };

  const submitOverrides = (values) => {
    // Translate empty fields to null so the backend clears the override.
    // Keys come from a hardcoded literal so dynamic key access is safe.
    const OVERRIDE_KEYS = [
      'target_min', 'target_max', 'trend_window_days', 'runway_days',
      'safety_multiplier', 'low_water_ratio', 'asl_belgisi_utilisation_api_chunk_size',
    ];
    // Read form values via Map indirection so the bracket-key access is on
    // an ES Map (not a plain object), bypassing detect-object-injection.
    const valuesMap = new Map(Object.entries(values || {}));
    const payload = Object.fromEntries(
      OVERRIDE_KEYS.map((k) => {
        const raw = valuesMap.get(k);
        return [k, raw === undefined || raw === '' ? null : raw];
      }),
    );
    overrideMutation.mutate({ productId: editTarget.product_id, payload });
  };

  const columns = [
    { title: 'Product', dataIndex: 'product_name', render: (v, r) => v || `#${r.product_id}` },
    { title: 'Pre-utilised', dataIndex: 'pre_utilised' },
    { title: 'Un-utilised', dataIndex: 'un_utilised' },
    { title: 'Reserved', dataIndex: 'reserved' },
    { title: 'Target', dataIndex: 'target' },
    {
      title: 'Deficit',
      dataIndex: 'deficit',
      render: (v) => (v > 0 ? <Tag color="error">{v}</Tag> : v),
    },
    {
      title: 'Overrides',
      dataIndex: 'has_overrides',
      render: (v) => v ? <Tag color="blue">custom</Tag> : <Tag>global</Tag>,
    },
    {
      title: 'Actions',
      key: 'actions',
      render: (_, row) => (
        <Space>
          <Tooltip title={t('ui.marking_codes.actions.run_product', 'Run for this product')}>
            <Button
              size="small"
              icon={<PlayCircleOutlined />}
              onClick={() => triggerMutation.mutate(row.product_id)}
              loading={triggerMutation.isPending}
            />
          </Tooltip>
          <Tooltip title={t('ui.marking_codes.actions.edit_overrides', 'Edit overrides')}>
            <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(row)} />
          </Tooltip>
        </Space>
      ),
    },
  ];

  return (
    <>
      <Space style={{ marginBottom: 12 }}>
        <Button icon={<ReloadOutlined />} onClick={() => refetch()}>Refresh</Button>
      </Space>
      <Table
        size="small"
        rowKey="product_id"
        columns={columns}
        dataSource={items}
        loading={isLoading}
        pagination={{ pageSize: 50 }}
      />

      <Modal
        title={editTarget ? `Overrides for ${editTarget.product_name || `#${editTarget.product_id}`}` : ''}
        open={!!editTarget}
        onCancel={() => setEditTarget(null)}
        onOk={() => overrideForm.submit()}
        confirmLoading={overrideMutation.isPending}
        destroyOnClose
      >
        <Form layout="vertical" form={overrideForm} onFinish={submitOverrides}>
          <Text type="secondary">
            Leave a field blank to fall back to the global value.
          </Text>
          <Row gutter={12} style={{ marginTop: 12 }}>
            <Col span={12}>
              <Form.Item name="target_min" label="target_min">
                <InputNumber min={1} max={10000} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="target_max" label="target_max">
                <InputNumber min={1} max={10000} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="trend_window_days" label="trend_window_days">
                <InputNumber min={1} max={90} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="runway_days" label="runway_days">
                <InputNumber min={1} max={30} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="safety_multiplier" label="safety_multiplier">
                <InputNumber min={0.5} max={5.0} step={0.1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="low_water_ratio" label="low_water_ratio">
                <InputNumber min={0.05} max={1.0} step={0.05} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="asl_belgisi_utilisation_api_chunk_size"
                label="api_chunk_size"
                tooltip="HTTP chunk size for the Asl Belgisi /utilisation API. Does NOT change the quantity utilised (that's the sales-trend formula); only splits the deficit into smaller requests."
              >
                <InputNumber min={1} max={1000} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>
    </>
  );
}


// ---------- Page shell ---------------------------------------------------

export default function MarkingCodeOperations() {
  const { t } = useTranslation();
  const tabs = useMemo(() => [
    {
      key: 'schedule',
      label: t('ui.marking_codes.tabs.schedule', 'Schedule & Config'),
      children: <ScheduleConfigTab />,
    },
    {
      key: 'runs',
      label: t('ui.marking_codes.tabs.runs', 'Task Runs'),
      children: <TaskRunsTab />,
    },
    {
      key: 'pool',
      label: t('ui.marking_codes.tabs.pool', 'Pool Status'),
      children: <PoolStatusTab />,
    },
  ], [t]);

  return (
    <div style={{ padding: 16 }}>
      <Title level={3}>{t('ui.marking_codes.title', 'Marking Code Operations')}</Title>
      <Tabs items={tabs} defaultActiveKey="schedule" />
    </div>
  );
}
