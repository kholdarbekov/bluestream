import React, { useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Divider,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  CheckOutlined,
  ClockCircleOutlined,
  DollarOutlined,
  ExclamationCircleOutlined,
  EyeOutlined,
  FileTextOutlined,
  ReloadOutlined,
  StopOutlined,
  ToolOutlined,
  UserOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import staffService from '../services/staffService';
import { usePermissions } from '../components/common/PermissionGuard';
import { formatDateTimeSeconds } from '../utils/dateUtils';

const ADJUSTABLE_SESSION_STATUSES = new Set(['submitted', 'partial', 'mismatch', 'overdue']);

const { Title, Text } = Typography;
const { Option } = Select;

const statusColor = (status, blocked) => {
  if (blocked) {
    return 'red';
  }
  if (status === 'verified' || status === 'resolved') {
    return 'green';
  }
  if (status === 'mismatch' || status === 'overdue') {
    return 'orange';
  }
  if (status === 'partial') {
    return 'gold';
  }
  if (status === 'submitted') {
    return 'blue';
  }
  if (status === 'force_closed') {
    return 'red';
  }
  return 'default';
};

const money = (value) => `${(value || 0).toLocaleString()} UZS`;
const VERIFY_REASON_OPTIONS = [
  { value: 'cash_count_matched', label: 'Cash count matched' },
  { value: 'cash_count_short', label: 'Cash short' },
  { value: 'cash_count_excess', label: 'Cash excess' },
  { value: 'manual_override', label: 'Manual override' },
  { value: 'evidence_reviewed', label: 'Evidence reviewed' },
];
const RESOLVE_REASON_OPTIONS = [
  { value: 'manager_approved_adjustment', label: 'Manager approved adjustment' },
  { value: 'cash_recovered_later', label: 'Cash recovered later' },
  { value: 'clerical_correction', label: 'Clerical correction' },
  { value: 'other', label: 'Other' },
];

const DeliveryReports = () => {
  const { t } = useTranslation(['staff', 'common']);
  const queryClient = useQueryClient();
  const { getUserRole, isAdmin } = usePermissions();
  const isSuperAdmin = getUserRole() === 'super_admin';
  const [period, setPeriod] = useState('day');
  const [statusFilter, setStatusFilter] = useState('all');
  const [blockedOnly, setBlockedOnly] = useState(false);
  const [warningOnly, setWarningOnly] = useState(false);
  const [selectedSessionId, setSelectedSessionId] = useState(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [verifyOpen, setVerifyOpen] = useState(false);
  const [resolveOpen, setResolveOpen] = useState(false);
  const [forceCloseOpen, setForceCloseOpen] = useState(false);
  const [verifyMode, setVerifyMode] = useState('approve');
  const [customerStatementId, setCustomerStatementId] = useState(null);
  const [orderTimelineId, setOrderTimelineId] = useState(null);
  const [recordCollectionOpen, setRecordCollectionOpen] = useState(false);
  const [recordCollectionCustomerId, setRecordCollectionCustomerId] = useState(null);
  const [adjustEvent, setAdjustEvent] = useState(null);
  const [verifyForm] = Form.useForm();
  const [resolveForm] = Form.useForm();
  const [forceCloseForm] = Form.useForm();
  const [recordCollectionForm] = Form.useForm();
  const [adjustForm] = Form.useForm();
  const collectionSource = Form.useWatch('source', recordCollectionForm) || 'standalone_meeting';
  const isPersonalCardTransfer = collectionSource === 'personal_card_transfer';
  const isBackfillCollection = collectionSource === 'backfill';

  const reportQueryKey = ['deliveryReports', period, statusFilter, blockedOnly, warningOnly];
  const { data, isLoading, refetch } = useQuery({
    queryKey: reportQueryKey,

    queryFn: () =>
      staffService.getCashReconciliation({
        period,
        ...(statusFilter !== 'all' ? { status: statusFilter } : {}),
        ...(blockedOnly ? { blocked_only: true } : {}),
        ...(warningOnly ? { warning_only: true } : {}),
      }),

    placeholderData: keepPreviousData,
  });

  const sessionDetailQuery = useQuery({
    queryKey: ['deliveryReportSession', selectedSessionId],
    queryFn: () => staffService.getCashReconciliationSession(selectedSessionId),
    enabled: Boolean(selectedSessionId) && detailOpen,
  });

  const customerStatementQuery = useQuery({
    queryKey: ['deliveryReportCustomerStatement', customerStatementId],
    queryFn: () => staffService.getCustomerCodStatement(customerStatementId),
    enabled: Boolean(customerStatementId),
  });

  const orderTimelineQuery = useQuery({
    queryKey: ['deliveryReportOrderTimeline', orderTimelineId],
    queryFn: () => staffService.getOrderPaymentTimeline(orderTimelineId),
    enabled: Boolean(orderTimelineId),
  });

  const driverOptionsQuery = useQuery({
    queryKey: ['deliveryReportDriversForCollections'],
    queryFn: () => staffService.getDeliveryPersons({ per_page: 100 }),
    enabled: recordCollectionOpen,
  });

  const recordCollectionStatementQuery = useQuery({
    queryKey: ['deliveryReportRecordCollectionStatement', recordCollectionCustomerId],
    queryFn: () => staffService.getCustomerCodStatement(recordCollectionCustomerId),
    enabled: recordCollectionOpen && Boolean(recordCollectionCustomerId),
  });

  const codDebtUsersQuery = useQuery({
    queryKey: ['deliveryReportCodDebtUsers', isPersonalCardTransfer, isBackfillCollection],

    queryFn: () => (
      isPersonalCardTransfer || isBackfillCollection
        ? staffService.searchCodCollectionUsers({
          q: '',
          type: 'phone',
          only_with_open_cod: false,
        })
        : staffService.getCodCollectionUsersWithOpenDebts({ limit: 500 })
    ),

    enabled: recordCollectionOpen,
  });

  const refreshReportQueries = () => {
    queryClient.invalidateQueries({
      queryKey: reportQueryKey,
    });
    if (selectedSessionId) {
      queryClient.invalidateQueries({
        queryKey: ['deliveryReportSession', selectedSessionId],
      });
    }
  };

  const verifyMutation = useMutation({
    mutationFn: ({ sessionId, payload }) => staffService.verifyCashReconciliationSession(sessionId, payload),

    onSuccess: () => {
      message.success(
        verifyMode === 'reject'
          ? t('staff:reconciliation_rejected', 'Reconciliation rejected and marked as mismatch')
          : t('staff:reconciliation_verified', 'Reconciliation verified')
      );
      setVerifyOpen(false);
      setVerifyMode('approve');
      verifyForm.resetFields();
      refreshReportQueries();
    },

    onError: (error) => {
      const backendMessage = error?.response?.data?.message;
      message.error(backendMessage || t('common:error_occurred'));
    },
  });

  const resolveMutation = useMutation({
    mutationFn: ({ sessionId, payload }) => staffService.resolveCashReconciliationSession(sessionId, payload),

    onSuccess: () => {
      message.success(t('staff:reconciliation_resolved', 'Reconciliation resolved'));
      setResolveOpen(false);
      resolveForm.resetFields();
      refreshReportQueries();
    },

    onError: (error) => {
      const backendMessage = error?.response?.data?.message;
      message.error(backendMessage || t('common:error_occurred'));
    },
  });

  const forceCloseMutation = useMutation({
    mutationFn: ({ sessionId, payload }) =>
      staffService.forceCloseCashReconciliationSession(sessionId, payload),

    onSuccess: () => {
      message.success(t('staff:reconciliation_force_closed', 'Session force-closed'));
      setForceCloseOpen(false);
      forceCloseForm.resetFields();
      refreshReportQueries();
    },

    onError: (error) => {
      const backendMessage = error?.response?.data?.message;
      message.error(backendMessage || t('common:error_occurred'));
    },
  });

  const recordCollectionMutation = useMutation({
    mutationFn: (payload) => staffService.recordCashCollection(payload),

    onSuccess: () => {
      message.success(t('staff:cash_collection_recorded', 'Cash collection recorded'));
      setRecordCollectionOpen(false);
      setRecordCollectionCustomerId(null);
      recordCollectionForm.resetFields();
      refreshReportQueries();
    },

    onError: (error) => {
      const backendMessage = error?.response?.data?.message;
      message.error(backendMessage || t('common:error_occurred'));
    },
  });

  const adjustMutation = useMutation({
    mutationFn: ({ eventId, payload }) => staffService.adjustCashCollectionEvent(eventId, payload),

    onSuccess: (response) => {
      const replacement = response?.data?.data?.cash_collection_event;
      message.success(
        t(
          'staff:cash_collection_event_adjusted',
          'Event adjusted. Replacement event #{{id}} created.',
          { id: replacement?.id ?? '' }
        )
      );
      setAdjustEvent(null);
      adjustForm.resetFields();
      refreshReportQueries();
    },

    onError: (error) => {
      const backendMessage = error?.response?.data?.message;
      message.error(backendMessage || t('common:error_occurred'));
    },
  });

  const payload = data?.data?.data || {};
  const summary = payload.summary || {};
  const report = payload.report || [];
  const sessions = payload.sessions || [];
  const sessionDetail = sessionDetailQuery.data?.data?.data || null;
  const customerStatement = customerStatementQuery.data?.data?.data || null;
  const orderTimeline = orderTimelineQuery.data?.data?.data || null;
  const recordCollectionStatement = recordCollectionStatementQuery.data?.data?.data || null;
  const codDebtUsers = codDebtUsersQuery.data?.data?.data?.items || [];
  const deliveryDrivers = driverOptionsQuery.data?.data?.data?.items || [];

  const selectedSession = useMemo(
    () => sessionDetail || sessions.find((session) => session.id === selectedSessionId) || null,
    [sessionDetail, sessions, selectedSessionId]
  );

  const canReviewSession = (session) => session.status === 'submitted';
  const canResolveSession = (session) => session.status === 'mismatch' || session.blocked_from_cod;

  const openAdjustModal = (event) => {
    setAdjustEvent(event);
    adjustForm.setFieldsValue({
      new_amount: Number(event.amount || 0),
      reason: '',
    });
  };

  const canAdjustEvent = (event) => {
    if (!isSuperAdmin) {
      return false;
    }
    if (!event || event.voided_at) {
      return false;
    }
    const sessionStatus = selectedSession?.status;
    if (!sessionStatus) {
      return false;
    }
    return ADJUSTABLE_SESSION_STATUSES.has(sessionStatus);
  };

  const openSessionDetail = (sessionId) => {
    setSelectedSessionId(sessionId);
    setDetailOpen(true);
  };

  const openVerifyModal = (session, mode = 'approve') => {
    setSelectedSessionId(session.id);
    setVerifyMode(mode);
    verifyForm.setFieldsValue({
      verified_cash:
        mode === 'reject'
          ? Math.max(0, (session.expected_cash_on_hand ?? session.expected_cash ?? session.declared_cash ?? 0) - 1)
          : (session.declared_cash ?? session.expected_cash_on_hand ?? session.expected_cash ?? 0),
      reason_code: mode === 'reject' ? 'cash_count_short' : 'cash_count_matched',
      notes: session.verification_notes || '',
    });
    setVerifyOpen(true);
  };

  const openResolveModal = (session) => {
    setSelectedSessionId(session.id);
    resolveForm.setFieldsValue({
      verified_cash: session.verified_cash ?? session.declared_cash ?? session.expected_cash_on_hand ?? session.expected_cash ?? 0,
      reason_code: session.resolution_reason_code || 'manager_approved_adjustment',
      resolution_notes: session.resolution_notes || '',
    });
    setResolveOpen(true);
  };

  const openForceCloseModal = (session) => {
    setSelectedSessionId(session.id);
    forceCloseForm.resetFields();
    setForceCloseOpen(true);
  };

  const canForceCloseSession = (session) =>
    isAdmin() && ['open', 'partial', 'overdue'].includes(session.status);

  const reportColumns = [
    {
      title: t('staff:driver_name'),
      dataIndex: 'driver_name',
      key: 'driver_name',
    },
    {
      title: t('staff:phone'),
      dataIndex: 'phone',
      key: 'phone',
    },
    {
      title: t('staff:cash_collected'),
      dataIndex: 'total_cash_collected',
      key: 'total_cash_collected',
      render: money,
    },
    {
      title: t('staff:blocked_sessions', 'Blocked'),
      dataIndex: 'blocked_session_count',
      key: 'blocked_session_count',
    },
    {
      title: t('staff:mismatch_sessions', 'Mismatches'),
      dataIndex: 'mismatch_session_count',
      key: 'mismatch_session_count',
    },
    {
      title: t('staff:warning_sessions', '7+ Day Warnings'),
      dataIndex: 'warning_session_count',
      key: 'warning_session_count',
    },
    {
      title: t('staff:submitted_sessions', 'Awaiting Count'),
      dataIndex: 'submitted_session_count',
      key: 'submitted_session_count',
    },
  ];

  const sessionColumns = [
    {
      title: t('staff:driver_name'),
      dataIndex: 'driver_name',
      key: 'driver_name',
    },
    {
      title: t('staff:session_started_at', 'Started'),
      dataIndex: 'session_started_at',
      key: 'session_started_at',
      render: (value) => (value ? formatDateTimeSeconds(value) : '—'),
    },
    {
      title: t('staff:session_age_days', 'Age'),
      dataIndex: 'session_age_days',
      key: 'session_age_days',
      render: (value) => `${value || 0}d`,
    },
    {
      title: t('staff:status', 'Status'),
      dataIndex: 'status',
      key: 'status',
      render: (value, record) => (
        <Tag color={statusColor(value, record.blocked_from_cod)}>
          {value}
        </Tag>
      ),
    },
    {
      title: t('staff:risk_flags', 'Risk'),
      dataIndex: 'risk_flags',
      key: 'risk_flags',
      render: (flags) => (
        <Space wrap>
          {(flags || []).map((flag) => (
            <Tag color="orange" key={flag}>{flag}</Tag>
          ))}
          {(!flags || flags.length === 0) ? '—' : null}
        </Space>
      ),
    },
    {
      title: t('staff:expected_cash', 'Expected Cash'),
      dataIndex: 'expected_cash_on_hand',
      key: 'expected_cash_on_hand',
      render: money,
    },
    {
      title: t('staff:warning_due_at', 'Warning Due'),
      dataIndex: 'warning_due_at',
      key: 'warning_due_at',
      render: (value, record) => (record.last_cash_activity_at ? (value ? formatDateTimeSeconds(value) : '—') : '—'),
    },
    {
      title: t('staff:declared_cash', 'Declared Cash'),
      dataIndex: 'declared_cash',
      key: 'declared_cash',
      render: (value) => (value == null ? '—' : money(value)),
    },
    {
      title: t('staff:verified_cash', 'Verified Cash'),
      dataIndex: 'verified_cash',
      key: 'verified_cash',
      render: (value) => (value == null ? '—' : money(value)),
    },
    {
      title: t('staff:variance', 'Variance'),
      dataIndex: 'verified_variance',
      key: 'verified_variance',
      render: (value, record) => money(value == null ? record.declared_variance : value),
    },
    {
      title: t('staff:actions', 'Actions'),
      key: 'actions',
      render: (_, record) => (
        <Space wrap>
          <Button icon={<EyeOutlined />} onClick={() => openSessionDetail(record.id)}>
            {t('common:view', 'View')}
          </Button>
          {canReviewSession(record) ? (
            <Button icon={<CheckOutlined />} type="primary" onClick={() => openVerifyModal(record, 'approve')}>
              {t('staff:approve', 'Approve')}
            </Button>
          ) : null}
          {canReviewSession(record) ? (
            <Button danger icon={<ExclamationCircleOutlined />} onClick={() => openVerifyModal(record, 'reject')}>
              {t('staff:reject', 'Reject')}
            </Button>
          ) : null}
          {canResolveSession(record) ? (
            <Button icon={<ToolOutlined />} onClick={() => openResolveModal(record)}>
              {t('staff:resolve', 'Resolve')}
            </Button>
          ) : null}
          {canForceCloseSession(record) ? (
            <Button danger icon={<StopOutlined />} onClick={() => openForceCloseModal(record)}>
              {t('staff:force_close', 'Force Close')}
            </Button>
          ) : null}
        </Space>
      ),
    },
  ];

  const eventColumns = [
    {
      title: t('staff:occurred_at', 'Occurred'),
      dataIndex: 'occurred_at',
      key: 'occurred_at',
      render: (value) => (value ? formatDateTimeSeconds(value) : '—'),
    },
    {
      title: t('staff:source', 'Source'),
      dataIndex: 'source',
      key: 'source',
      render: (value) => <Tag>{value}</Tag>,
    },
    {
      title: t('staff:customer', 'Customer'),
      key: 'customer',
      render: (_, record) =>
        record.customer_name || record.customer_phone ? (
          <Space direction="vertical" size={0}>
            <Text>{record.customer_name || '—'}</Text>
            {record.customer_phone ? (
              <Text type="secondary" style={{ fontSize: 12 }}>{record.customer_phone}</Text>
            ) : null}
          </Space>
        ) : (
          '—'
        ),
    },
    {
      title: t('staff:amount', 'Amount'),
      key: 'amount',
      render: (_, record) => {
        const amount = money(record.amount);
        if (record.voided_at) {
          const replacementId = record.entry_metadata?.adjusted_replacement_event_id;
          return (
            <Space direction="vertical" size={0}>
              <Text delete>{amount}</Text>
              <Tag color="default">
                {replacementId
                  ? t('staff:event_replaced_by', 'Replaced by #{{id}}', { id: replacementId })
                  : t('staff:event_voided', 'Voided')}
              </Tag>
            </Space>
          );
        }
        const originalId = record.entry_metadata?.original_event_id;
        return (
          <Space direction="vertical" size={0}>
            <Text>{amount}</Text>
            {originalId ? (
              <Tag color="blue">
                {t('staff:event_adjusted_from', 'Adjusted from #{{id}}', { id: originalId })}
              </Tag>
            ) : null}
          </Space>
        );
      },
    },
    {
      title: t('staff:notes', 'Notes'),
      dataIndex: 'notes',
      key: 'notes',
      render: (value) => value || '—',
    },
    {
      title: t('staff:actions', 'Actions'),
      key: 'actions',
      render: (_, record) => (
        <Space wrap>
          {record.customer_id ? (
            <Button
              size="small"
              icon={<UserOutlined />}
              onClick={() => setCustomerStatementId(record.customer_id)}
            >
              {t('staff:customer_statement', 'Customer Statement')}
            </Button>
          ) : null}
          {record.order_id ? (
            <Button
              size="small"
              icon={<FileTextOutlined />}
              onClick={() => setOrderTimelineId(record.order_id)}
            >
              {t('staff:payment_timeline', 'Payment Timeline')}
            </Button>
          ) : null}
          {canAdjustEvent(record) ? (
            <Button
              size="small"
              icon={<ToolOutlined />}
              onClick={() => openAdjustModal(record)}
            >
              {t('staff:adjust_event_amount', 'Adjust amount')}
            </Button>
          ) : null}
        </Space>
      ),
    },
  ];

  const renderSettlementBreakdown = (record) => {
    const allocations = record.allocations || [];
    const columns = [
      { title: t('staff:order_number', 'Order'), dataIndex: 'order_number', key: 'order_number',
        render: (value) => value || '—' },
      { title: t('staff:allocated', 'Allocated'), dataIndex: 'allocated_amount', key: 'allocated_amount',
        render: (value) => money(value) },
      {
        title: t('staff:result', 'Result'),
        key: 'result',
        render: (_, alloc) => (
          <Space size={4} wrap>
            {alloc.settlement === 'fully' ? (
              <Tag color="green">{t('staff:fully_paid', '✓ Fully paid')}</Tag>
            ) : (
              <Tag color="orange">{t('staff:partially_paid', '◐ Partially paid')}</Tag>
            )}
            {alloc.reversed ? <Tag color="red">{t('staff:reversed', 'Reversed')}</Tag> : null}
          </Space>
        ),
      },
      {
        title: t('staff:actions', 'Actions'),
        key: 'actions',
        render: (_, alloc) =>
          alloc.order_id ? (
            <Button size="small" icon={<FileTextOutlined />} onClick={() => setOrderTimelineId(alloc.order_id)}>
              {t('staff:payment_timeline', 'Payment Timeline')}
            </Button>
          ) : null,
      },
    ];
    return (
      <Table
        columns={columns}
        dataSource={allocations}
        rowKey={(row, index) => `${row.order_id ?? 'na'}-${index}`}
        pagination={false}
        size="small"
      />
    );
  };

  const statementColumns = [
    {
      title: t('staff:order_number', 'Order'),
      dataIndex: 'order_number',
      key: 'order_number',
    },
    {
      title: t('staff:status', 'Status'),
      dataIndex: 'status',
      key: 'status',
      render: (value) => <Tag>{value}</Tag>,
    },
    {
      title: t('staff:amount', 'Amount'),
      dataIndex: 'amount',
      key: 'amount',
      render: money,
    },
    {
      title: t('staff:cash_collected', 'Collected'),
      dataIndex: 'amount_collected',
      key: 'amount_collected',
      render: money,
    },
    {
      title: t('staff:outstanding_amount', 'Outstanding'),
      dataIndex: 'outstanding_amount',
      key: 'outstanding_amount',
      render: money,
    },
  ];

  const timelineColumns = [
    {
      title: t('staff:type', 'Type'),
      dataIndex: 'type',
      key: 'type',
    },
    {
      title: t('staff:timestamp', 'Timestamp'),
      dataIndex: 'timestamp',
      key: 'timestamp',
      render: (value) => (value ? formatDateTimeSeconds(value) : '—'),
    },
    {
      title: t('staff:amount', 'Amount'),
      key: 'amount',
      render: (_, record) => {
        const value = record.allocated_amount ?? record.amount ?? 0;
        return money(value);
      },
    },
    {
      title: t('staff:notes', 'Notes'),
      dataIndex: 'notes',
      key: 'notes',
      render: (value) => value || '—',
    },
  ];

  return (
    <div>
      <Row justify="space-between" align="middle" style={{ marginBottom: 24 }}>
        <Col>
          <Title level={3}>{t('staff:cash_reconciliation', 'Delivery Reports')}</Title>
        </Col>
        <Col>
          <Space>
            <Select value={period} onChange={setPeriod} style={{ width: 140 }}>
              <Option value="day">{t('staff:today')}</Option>
              <Option value="week">{t('staff:this_week')}</Option>
              <Option value="month">{t('staff:this_month')}</Option>
            </Select>
            <Select value={statusFilter} onChange={setStatusFilter} style={{ width: 160 }}>
              <Option value="all">{t('staff:all_statuses', 'All statuses')}</Option>
              <Option value="open">{t('staff:open', 'Open')}</Option>
              <Option value="partial">{t('staff:partial', 'Partial')}</Option>
              <Option value="submitted">{t('staff:submitted', 'Submitted')}</Option>
              <Option value="verified">{t('staff:verified', 'Verified')}</Option>
              <Option value="mismatch">{t('staff:mismatch', 'Mismatch')}</Option>
              <Option value="overdue">{t('staff:overdue', 'Overdue')}</Option>
              <Option value="resolved">{t('staff:resolved', 'Resolved')}</Option>
            </Select>
            <Button
              type={blockedOnly ? 'primary' : 'default'}
              icon={<WarningOutlined />}
              onClick={() => {
                setBlockedOnly((value) => !value);
                setWarningOnly(false);
              }}
            >
              {blockedOnly
                ? t('staff:showing_blocked_only', 'Blocked only')
                : t('staff:filter_blocked', 'Filter blocked')}
            </Button>
            <Button
              type={warningOnly ? 'primary' : 'default'}
              icon={<ClockCircleOutlined />}
              onClick={() => {
                setWarningOnly((value) => !value);
                setBlockedOnly(false);
              }}
            >
              {warningOnly
                ? t('staff:showing_warning_only', '7+ day warnings')
                : t('staff:filter_warning', 'Filter warnings')}
            </Button>
            <Button icon={<ReloadOutlined />} onClick={() => refetch()}>
              {t('common:refresh')}
            </Button>
            <Button
              type="primary"
              icon={<DollarOutlined />}
              onClick={() => {
                setRecordCollectionCustomerId(null);
                recordCollectionForm.resetFields();
                setRecordCollectionOpen(true);
              }}
            >
              {t('staff:record_cash_collection', 'Record Collection')}
            </Button>
          </Space>
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={12} md={4}>
          <Card>
            <Statistic
              title={t('staff:grand_total')}
              value={payload.grand_total_cash || 0}
              prefix={<DollarOutlined />}
              suffix="UZS"
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} md={4}>
          <Card>
            <Statistic
              title={t('staff:active_sessions', 'Active')}
              value={summary.open_session_count || 0}
              prefix={<ClockCircleOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} md={4}>
          <Card>
            <Statistic
              title={t('staff:submitted_sessions', 'Awaiting Count')}
              value={summary.submitted_session_count || 0}
              prefix={<CheckOutlined />}
              valueStyle={{ color: '#1677ff' }}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} md={4}>
          <Card>
            <Statistic
              title={t('staff:warning_sessions', '7+ Day Warnings')}
              value={summary.warning_session_count || 0}
              prefix={<ClockCircleOutlined />}
              valueStyle={{ color: '#fa8c16' }}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} md={4}>
          <Card>
            <Statistic
              title={t('staff:mismatch_sessions', 'Mismatches')}
              value={summary.mismatch_session_count || 0}
              prefix={<ExclamationCircleOutlined />}
              valueStyle={{ color: '#faad14' }}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} md={4}>
          <Card>
            <Statistic
              title={t('staff:blocked_sessions', 'Blocked Sessions')}
              value={summary.blocked_session_count || 0}
              prefix={<WarningOutlined />}
              valueStyle={{ color: '#ff4d4f' }}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} md={4}>
          <Card>
            <Statistic
              title={t('staff:resolved_verified_sessions', 'Resolved / Verified')}
              value={(summary.verified_session_count || 0) + (summary.resolved_session_count || 0)}
              prefix={<CheckOutlined />}
              valueStyle={{ color: '#389e0d' }}
            />
          </Card>
        </Col>
      </Row>

      <Card title={t('staff:cash_reconciliation', 'Driver Reconciliation')}>
        <Table
          columns={reportColumns}
          dataSource={report}
          rowKey="driver_id"
          loading={isLoading}
          pagination={false}
          scroll={{ x: 900 }}
        />
      </Card>

      <Card title={t('staff:open_sessions', 'Session Drill-Down')} style={{ marginTop: 24 }}>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={t(
            'staff:delivery_reports_admin_hint',
            'Submitted sessions are ready for cashier/admin verification. 7+ day sessions are warnings only; mismatch sessions still block COD until resolved.'
          )}
        />
        <Table
          columns={sessionColumns}
          dataSource={sessions}
          rowKey="id"
          loading={isLoading}
          pagination={false}
          scroll={{ x: 1320 }}
        />
      </Card>

      <Modal
        title={t('staff:session_detail', 'Reconciliation Session')}
        open={detailOpen}
        onCancel={() => setDetailOpen(false)}
        footer={null}
        width={1360}
      >
        {sessionDetailQuery.isLoading ? (
          <Text>{t('common:loading', 'Loading...')}</Text>
        ) : selectedSession ? (
          <>
            <Descriptions bordered column={2} size="small">
              <Descriptions.Item label={t('staff:driver_name')}>{selectedSession.driver_name || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('staff:status', 'Status')}>
                <Tag color={statusColor(selectedSession.status, selectedSession.blocked_from_cod)}>
                  {selectedSession.status}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t('staff:session_started_at', 'Started')}>
                {selectedSession.session_started_at ? formatDateTimeSeconds(selectedSession.session_started_at) : '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('staff:session_ended_at', 'Ended')}>
                {selectedSession.session_ended_at ? formatDateTimeSeconds(selectedSession.session_ended_at) : '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('staff:session_age_days', 'Session Age')}>
                {selectedSession.session_age_days == null ? '—' : `${selectedSession.session_age_days}d`}
              </Descriptions.Item>
              <Descriptions.Item label={t('staff:expected_cash', 'Expected Cash')}>{money(selectedSession.expected_cash)}</Descriptions.Item>
              <Descriptions.Item label={t('staff:expected_cash_on_hand', 'Expected On Hand')}>
                {money(selectedSession.expected_cash_on_hand)}
              </Descriptions.Item>
              <Descriptions.Item label={t('staff:declared_cash', 'Declared Cash')}>
                {selectedSession.declared_cash == null ? '—' : money(selectedSession.declared_cash)}
              </Descriptions.Item>
              <Descriptions.Item label={t('staff:verified_cash', 'Verified Cash')}>
                {selectedSession.verified_cash == null ? '—' : money(selectedSession.verified_cash)}
              </Descriptions.Item>
              <Descriptions.Item label={t('staff:declared_variance', 'Declared Variance')}>{money(selectedSession.declared_variance)}</Descriptions.Item>
              <Descriptions.Item label={t('staff:verified_variance', 'Verified Variance')}>{money(selectedSession.verified_variance)}</Descriptions.Item>
              <Descriptions.Item label={t('staff:block_reason', 'Block Reason')}>{selectedSession.block_reason || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('staff:force_close_reason', 'Force Close Reason')}>
                {selectedSession.force_close_reason || '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('staff:event_count', 'Collection Events')}>{selectedSession.event_count || 0}</Descriptions.Item>
              <Descriptions.Item label={t('staff:warning_due_at', 'Warning Due')}>
                {selectedSession.last_cash_activity_at
                  ? (selectedSession.warning_due_at ? formatDateTimeSeconds(selectedSession.warning_due_at) : '—')
                  : '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('staff:last_cash_activity_at', 'Last Cash Activity')}>
                {selectedSession.last_cash_activity_at ? formatDateTimeSeconds(selectedSession.last_cash_activity_at) : '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('staff:risk_flags', 'Risk Flags')}>
                <Space wrap>
                  {(selectedSession.risk_flags || []).map((flag) => (
                    <Tag color="orange" key={flag}>{flag}</Tag>
                  ))}
                  {(!selectedSession.risk_flags || selectedSession.risk_flags.length === 0) ? '—' : null}
                </Space>
              </Descriptions.Item>
            </Descriptions>

            {selectedSession.blocked_from_cod ? (
              <Alert
                type="warning"
                showIcon
                style={{ marginTop: 16 }}
                message={t('staff:driver_blocked_from_cod', 'Driver is blocked from new COD work until this session is cleared.')}
              />
            ) : null}

            <Divider>{t('staff:cash_handoffs', 'Cash Handoffs')}</Divider>
            <Table
              columns={[
                {
                  title: t('staff:handoff_occurred_at', 'When'),
                  dataIndex: 'occurred_at',
                  key: 'occurred_at',
                  render: (value) => (value ? formatDateTimeSeconds(value) : '—'),
                },
                {
                  title: t('staff:handoff_amount', 'Amount'),
                  dataIndex: 'amount',
                  key: 'amount',
                  render: (value) => money(value),
                },
                {
                  title: t('staff:handoff_notes', 'Notes'),
                  dataIndex: 'notes',
                  key: 'notes',
                  render: (value) => value || '—',
                },
              ]}
              dataSource={selectedSession.handoffs || []}
              rowKey="id"
              pagination={false}
              size="small"
              locale={{ emptyText: t('staff:no_handoffs_yet', 'No handoffs yet.') }}
            />

            <Divider>{t('staff:collection_events', 'Collection Events')}</Divider>
            <Table
              columns={eventColumns}
              dataSource={selectedSession.events || []}
              rowKey="id"
              pagination={false}
              expandable={{
                expandedRowRender: renderSettlementBreakdown,
                rowExpandable: (record) => (record.allocations?.length ?? 0) > 0,
              }}
            />
          </>
        ) : null}
      </Modal>

      <Modal
        title={
          verifyMode === 'reject'
            ? t('staff:reject_reconciliation', 'Reject Reconciliation')
            : t('staff:approve_reconciliation', 'Approve Reconciliation')
        }
        open={verifyOpen}
        onCancel={() => {
          setVerifyOpen(false);
          setVerifyMode('approve');
        }}
        onOk={() => verifyForm.submit()}
        confirmLoading={verifyMutation.isPending}
      >
        <Alert
          type={verifyMode === 'reject' ? 'warning' : 'info'}
          showIcon
          style={{ marginBottom: 16 }}
          message={
            verifyMode === 'reject'
              ? t(
                  'staff:reject_reconciliation_help',
                  'Rejecting sends the session into mismatch status and keeps the driver blocked from new COD work until it is resolved.'
                )
              : t(
                  'staff:approve_reconciliation_help',
                  'Approving verifies the session. If the verified cash differs from expected cash, the session will still move into mismatch status.'
                )
          }
        />
        <Form
          form={verifyForm}
          layout="vertical"
          onFinish={(values) => {
            const expectedCash = Number(
              selectedSession?.expected_cash_on_hand ?? selectedSession?.expected_cash ?? 0
            );
            const verifiedCash = Number(values.verified_cash ?? 0);
            if (
              verifyMode === 'reject' &&
              selectedSession &&
              verifiedCash === expectedCash
            ) {
              message.error(
                t(
                  'staff:reject_requires_difference',
                  'Rejected reconciliations must use a verified cash amount that differs from expected cash.'
                )
              );
              return;
            }
            if (verifiedCash !== expectedCash && !values.notes) {
              message.error(
                t(
                  'staff:variance_notes_required',
                  'Notes are required when verified cash differs from expected cash.'
                )
              );
              return;
            }

            verifyMutation.mutate({
              sessionId: selectedSessionId,
              payload: values,
            });
          }}
        >
          <Form.Item
            name="verified_cash"
            label={t('staff:verified_cash', 'Verified Cash')}
            rules={[{ required: true, message: t('staff:verified_cash_required', 'Verified cash is required') }]}
          >
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item
            name="reason_code"
            label={t('staff:reason_code', 'Reason Code')}
            rules={[{ required: true, message: t('staff:reason_code_required', 'Reason code is required') }]}
          >
            <Select>
              {VERIFY_REASON_OPTIONS.map((option) => (
                <Option value={option.value} key={option.value}>{option.label}</Option>
              ))}
            </Select>
          </Form.Item>
          <Form.Item
            name="notes"
            label={t('staff:notes', 'Notes')}
            rules={
              verifyMode === 'reject'
                ? [{ required: true, message: t('staff:rejection_notes_required', 'Rejection notes are required') }]
                : []
            }
          >
            <Input.TextArea rows={3} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t('staff:resolve', 'Resolve Session')}
        open={resolveOpen}
        onCancel={() => setResolveOpen(false)}
        onOk={() => resolveForm.submit()}
        confirmLoading={resolveMutation.isPending}
      >
        <Form
          form={resolveForm}
          layout="vertical"
          onFinish={(values) =>
            resolveMutation.mutate({
              sessionId: selectedSessionId,
              payload: values,
            })
          }
        >
          <Form.Item name="verified_cash" label={t('staff:verified_cash', 'Verified Cash')}>
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item
            name="reason_code"
            label={t('staff:reason_code', 'Reason Code')}
            rules={[{ required: true, message: t('staff:reason_code_required', 'Reason code is required') }]}
          >
            <Select>
              {RESOLVE_REASON_OPTIONS.map((option) => (
                <Option value={option.value} key={option.value}>{option.label}</Option>
              ))}
            </Select>
          </Form.Item>
          <Form.Item
            name="resolution_notes"
            label={t('staff:resolution_notes', 'Resolution Notes')}
            rules={[{ required: true, message: t('staff:resolution_notes_required', 'Resolution notes are required') }]}
          >
            <Input.TextArea rows={4} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t('staff:force_close_session_title', 'Force Close Session')}
        open={forceCloseOpen}
        onCancel={() => setForceCloseOpen(false)}
        onOk={() => forceCloseForm.submit()}
        confirmLoading={forceCloseMutation.isPending}
        okButtonProps={{ danger: true }}
        okText={t('staff:force_close', 'Force Close')}
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={t(
            'staff:force_close_help',
            'Force-closing settles this active session administratively and unblocks the driver from COD. Use for abandoned or stuck sessions the driver cannot close themselves.'
          )}
        />
        <Form
          form={forceCloseForm}
          layout="vertical"
          onFinish={(values) =>
            forceCloseMutation.mutate({
              sessionId: selectedSessionId,
              payload: { reason: values.reason, verified_cash: values.verified_cash },
            })
          }
        >
          <Form.Item
            name="reason"
            label={t('staff:reason', 'Reason')}
            rules={[{ required: true, message: t('staff:force_close_reason_required', 'A reason is required') }]}
          >
            <Input.TextArea rows={3} />
          </Form.Item>
          <Form.Item
            name="verified_cash"
            label={t('staff:verified_cash_optional', 'Verified cash counted (optional)')}
          >
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t('staff:customer_statement', 'Customer COD Statement')}
        open={Boolean(customerStatementId)}
        onCancel={() => setCustomerStatementId(null)}
        footer={null}
        width={900}
      >
        {customerStatementQuery.isLoading ? (
          <Text>{t('common:loading', 'Loading...')}</Text>
        ) : customerStatement ? (
          <>
            <Descriptions bordered column={2} size="small" style={{ marginBottom: 16 }}>
              <Descriptions.Item label={t('staff:customer', 'Customer')}>
                {`${customerStatement.first_name || ''} ${customerStatement.last_name || ''}`.trim() || '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('staff:customer_phone', 'Phone')}>
                {customerStatement.phone || '—'}
              </Descriptions.Item>
            </Descriptions>
            <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
              <Col span={8}>
                <Statistic title={t('staff:active_cod_debts', 'Active COD Debts')} value={customerStatement.active_cod_debt_count || 0} />
              </Col>
              <Col span={8}>
                <Statistic title={t('staff:outstanding_amount', 'Outstanding')} value={customerStatement.total_outstanding_amount || 0} suffix="UZS" />
              </Col>
              <Col span={8}>
                <Statistic title={t('staff:cod_restricted', 'COD Restricted')} value={customerStatement.cod_restricted ? t('common:yes', 'Yes') : t('common:no', 'No')} />
              </Col>
            </Row>
            <Table
              columns={statementColumns}
              dataSource={customerStatement.items || []}
              rowKey="payment_id"
              pagination={false}
              scroll={{ x: 720 }}
            />
          </>
        ) : null}
      </Modal>

      <Modal
        title={t('staff:payment_timeline', 'Order Payment Timeline')}
        open={Boolean(orderTimelineId)}
        onCancel={() => setOrderTimelineId(null)}
        footer={null}
        width={900}
      >
        {orderTimelineQuery.isLoading ? (
          <Text>{t('common:loading', 'Loading...')}</Text>
        ) : orderTimeline ? (
          <>
            <Descriptions bordered column={2} size="small" style={{ marginBottom: 16 }}>
              <Descriptions.Item label={t('staff:order_number', 'Order')}>{orderTimeline.order_number}</Descriptions.Item>
              <Descriptions.Item label={t('staff:customer', 'Customer')}>
                {orderTimeline.customer_name || '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('staff:customer_phone', 'Phone')}>
                {orderTimeline.customer_phone || '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('staff:status', 'Status')}>
                <Tag>{orderTimeline.status}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t('staff:amount', 'Amount')}>{money(orderTimeline.amount)}</Descriptions.Item>
              <Descriptions.Item label={t('staff:cash_collected', 'Collected')}>{money(orderTimeline.amount_collected)}</Descriptions.Item>
              <Descriptions.Item label={t('staff:outstanding_amount', 'Outstanding')}>{money(orderTimeline.outstanding_amount)}</Descriptions.Item>
              <Descriptions.Item label={t('staff:payment_id', 'Payment ID')}>{orderTimeline.payment_id}</Descriptions.Item>
            </Descriptions>
            <Table
              columns={timelineColumns}
              dataSource={orderTimeline.timeline || []}
              rowKey={(record, index) => `${record.type}-${record.timestamp || index}`}
              pagination={false}
              scroll={{ x: 720 }}
            />
          </>
        ) : null}
      </Modal>

      <Modal
        title={t('staff:record_cash_collection', 'Record Collection')}
        open={recordCollectionOpen}
        onCancel={() => {
          setRecordCollectionOpen(false);
          setRecordCollectionCustomerId(null);
          recordCollectionForm.resetFields();
        }}
        onOk={() => recordCollectionForm.submit()}
        confirmLoading={recordCollectionMutation.isPending}
      >
        <Form
          form={recordCollectionForm}
          layout="vertical"
          initialValues={{ source: 'standalone_meeting' }}
          onFinish={(values) => {
            if (
              values.source !== 'admin_adjustment' &&
              values.source !== 'backfill' &&
              values.source !== 'personal_card_transfer' &&
              recordCollectionStatement &&
              Number(recordCollectionStatement.active_cod_debt_count || 0) <= 0
            ) {
              message.error(
                t(
                  'staff:no_open_cod_for_collection',
                  'This customer has no active COD debt to collect. Use an admin correction instead if this is an adjustment.'
                )
              );
              return;
            }

            recordCollectionMutation.mutate({
              ...values,
              customer_id: values.customer_id,
              collector_user_id: values.collector_user_id || null,
              driver_cash_session_id: values.driver_cash_session_id || null,
              order_id: values.order_id || null,
              proof_data: { channel: 'admin_ui_delivery_reports' },
            });
          }}
        >
          <Form.Item
            name="customer_id"
            label={t('staff:customer', 'Customer')}
            rules={[{ required: true, message: t('staff:customer_required', 'Customer is required') }]}
          >
            <Select
              showSearch
              optionFilterProp="children"
              filterOption
              loading={codDebtUsersQuery.isLoading}
              onChange={(value) => setRecordCollectionCustomerId(value)}
              placeholder={isPersonalCardTransfer
                ? t('staff:search_customer_any', 'Select customer')
                : (
                  isBackfillCollection
                    ? t('staff:search_customer_any', 'Select customer')
                    : t('staff:search_customer', 'Select user with COD debt')
                )}
            >
              {codDebtUsers.map((customer) => (
                <Option key={customer.id} value={customer.id}>
                  {customer.first_name} {customer.last_name} - {customer.phone} | {t('staff:active_cod_debts', 'Active COD debts')}: {customer.active_cod_debt_count || 0}
                </Option>
              ))}
            </Select>
          </Form.Item>

          {recordCollectionStatement ? (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message={t(
                'staff:collection_statement_summary',
                'Active COD debts: {{count}} | Outstanding: {{amount}} UZS',
                {
                  count: recordCollectionStatement.active_cod_debt_count || 0,
                  amount: (recordCollectionStatement.total_outstanding_amount || 0).toLocaleString(),
                }
              )}
            />
          ) : null}

          {isPersonalCardTransfer ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 16 }}
              message={t(
                'staff:personal_card_transfer_help',
                'Personal card transfer: this records non-cash settlement and does not assign driver cash custody.'
              )}
            />
          ) : null}

          <Form.Item
            name="source"
            label={t('staff:collection_type', 'Collection Type')}
            rules={[{ required: true }]}
          >
            <Select
              onChange={(value) => {
                if (value === 'admin_adjustment' || value === 'personal_card_transfer') {
                  recordCollectionForm.setFieldsValue({ collector_user_id: undefined });
                }
              }}
            >
              <Option value="standalone_meeting">{t('staff:standalone_office_collection', 'Standalone / office collection')}</Option>
              <Option value="admin_adjustment">{t('staff:admin_correction', 'Admin correction')}</Option>
              <Option value="backfill">{t('staff:backfill_collection', 'Backfill to existing session')}</Option>
              <Option value="personal_card_transfer">{t('staff:personal_card_transfer', 'Personal card transfer')}</Option>
            </Select>
          </Form.Item>

          <Form.Item name="collector_user_id" label={t('staff:collector_driver', 'Collector Driver')}>
            <Select
              allowClear
              showSearch
              optionFilterProp="children"
              disabled={collectionSource === 'admin_adjustment' || isPersonalCardTransfer}
              placeholder={t('staff:collector_driver_optional', 'Optional driver attribution')}
            >
              {deliveryDrivers.map((driver) => (
                <Option key={driver.user_id || driver.id} value={driver.user_id || driver.id}>
                  {driver.full_name || driver.name} - {driver.phone}
                </Option>
              ))}
            </Select>
          </Form.Item>

          {collectionSource === 'backfill' ? (
            <Form.Item
              name="driver_cash_session_id"
              label={t('staff:driver_cash_session_id', 'Driver Cash Session ID')}
              rules={[{
                required: true,
                message: t('staff:driver_cash_session_id_required', 'Driver cash session is required for backfill'),
              }]}
            >
              <InputNumber min={1} style={{ width: '100%' }} />
            </Form.Item>
          ) : null}

          <Form.Item
            name="order_id"
            label={isPersonalCardTransfer
              ? t('staff:target_order_required', 'Target Order')
              : t('staff:target_order_optional', 'Target Order (optional)')}
            rules={isPersonalCardTransfer
              ? [{ required: true, message: t('staff:target_order_required_error', 'Target order is required for personal card transfers') }]
              : []}
          >
            <Select
              allowClear
              placeholder={t('staff:auto_allocate_oldest_first', 'Leave blank to auto-allocate oldest debt first')}
              disabled={!recordCollectionStatement?.items?.length}
            >
              {(recordCollectionStatement?.items || [])
                .filter((item) => Number(item.outstanding_amount || 0) > 0)
                .map((item) => (
                  <Option key={item.order_id} value={item.order_id}>
                    {item.order_number} - {money(item.outstanding_amount)}
                  </Option>
                ))}
            </Select>
          </Form.Item>

          <Form.Item
            name="amount"
            label={t('staff:amount', 'Amount')}
            rules={[{ required: true, message: t('staff:amount_required', 'Amount is required') }]}
          >
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            name="notes"
            label={t('staff:notes', 'Notes')}
            rules={[{ required: true, message: t('staff:collection_notes_required', 'Notes are required') }]}
          >
            <Input.TextArea rows={4} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t('staff:adjust_event_modal_title', 'Adjust cash collection event')}
        open={Boolean(adjustEvent)}
        onCancel={() => {
          setAdjustEvent(null);
          adjustForm.resetFields();
        }}
        onOk={() => adjustForm.submit()}
        confirmLoading={adjustMutation.isPending}
      >
        {adjustEvent ? (
          <>
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 16 }}
              message={t(
                'staff:adjust_event_help',
                'Adjusting voids the original event and creates a replacement with the corrected amount. Any prepayment auto-applied from the surplus will be reversed and re-applied based on the new amount.'
              )}
            />
            <Descriptions size="small" column={1} bordered style={{ marginBottom: 16 }}>
              <Descriptions.Item label={t('staff:current_amount', 'Current amount')}>
                {money(adjustEvent.amount)}
              </Descriptions.Item>
              <Descriptions.Item label={t('staff:applied_to_payments', 'Applied to payments')}>
                {money(Number(adjustEvent.amount || 0) - Number(adjustEvent.unapplied_amount || 0))}
              </Descriptions.Item>
              <Descriptions.Item label={t('staff:customer_prepayment', 'Customer prepayment (unapplied)')}>
                {money(adjustEvent.unapplied_amount)}
              </Descriptions.Item>
            </Descriptions>
            <Form
              form={adjustForm}
              layout="vertical"
              onFinish={(values) => {
                adjustMutation.mutate({
                  eventId: adjustEvent.id,
                  payload: {
                    new_amount: Number(values.new_amount),
                    reason: values.reason,
                  },
                });
              }}
            >
              <Form.Item
                name="new_amount"
                label={t('staff:adjust_event_new_amount', 'Corrected amount')}
                rules={[
                  { required: true, message: t('staff:adjust_event_new_amount_required', 'Corrected amount is required') },
                  {
                    validator: (_, value) =>
                      value && Number(value) > 0
                        ? Promise.resolve()
                        : Promise.reject(new Error(t('staff:adjust_event_amount_positive', 'Amount must be positive'))),
                  },
                ]}
              >
                <InputNumber min={1} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item
                name="reason"
                label={t('staff:adjust_event_reason', 'Reason (min 5 characters)')}
                rules={[
                  { required: true, message: t('staff:adjust_event_reason_required', 'Reason is required') },
                  { min: 5, message: t('staff:adjust_event_reason_min', 'Reason must be at least 5 characters') },
                ]}
              >
                <Input.TextArea rows={3} />
              </Form.Item>
            </Form>
          </>
        ) : null}
      </Modal>
    </div>
  );
};

export default DeliveryReports;
