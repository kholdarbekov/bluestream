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
  ToolOutlined,
  UserOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from 'react-query';
import { useTranslation } from 'react-i18next';
import adminService from '../services/adminService';
import staffService from '../services/staffService';

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
  if (status === 'submitted') {
    return 'blue';
  }
  return 'default';
};

const money = (value) => `${(value || 0).toLocaleString()} UZS`;

const DeliveryReports = () => {
  const { t } = useTranslation(['staff', 'common']);
  const queryClient = useQueryClient();
  const [period, setPeriod] = useState('day');
  const [statusFilter, setStatusFilter] = useState('all');
  const [blockedOnly, setBlockedOnly] = useState(false);
  const [selectedSessionId, setSelectedSessionId] = useState(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [verifyOpen, setVerifyOpen] = useState(false);
  const [resolveOpen, setResolveOpen] = useState(false);
  const [verifyMode, setVerifyMode] = useState('approve');
  const [customerStatementId, setCustomerStatementId] = useState(null);
  const [orderTimelineId, setOrderTimelineId] = useState(null);
  const [recordCollectionOpen, setRecordCollectionOpen] = useState(false);
  const [recordCollectionCustomerId, setRecordCollectionCustomerId] = useState(null);
  const [customerOptions, setCustomerOptions] = useState([]);
  const [customerSearchLoading, setCustomerSearchLoading] = useState(false);
  const [verifyForm] = Form.useForm();
  const [resolveForm] = Form.useForm();
  const [recordCollectionForm] = Form.useForm();
  const collectionSource = Form.useWatch('source', recordCollectionForm) || 'standalone_meeting';

  const reportQueryKey = ['deliveryReports', period, statusFilter, blockedOnly];
  const { data, isLoading, refetch } = useQuery(
    reportQueryKey,
    () =>
      staffService.getCashReconciliation({
        period,
        ...(statusFilter !== 'all' ? { status: statusFilter } : {}),
        ...(blockedOnly ? { blocked_only: true } : {}),
      }),
    { keepPreviousData: true }
  );

  const sessionDetailQuery = useQuery(
    ['deliveryReportSession', selectedSessionId],
    () => staffService.getCashReconciliationSession(selectedSessionId),
    { enabled: !!selectedSessionId && detailOpen }
  );

  const customerStatementQuery = useQuery(
    ['deliveryReportCustomerStatement', customerStatementId],
    () => staffService.getCustomerCodStatement(customerStatementId),
    { enabled: !!customerStatementId }
  );

  const orderTimelineQuery = useQuery(
    ['deliveryReportOrderTimeline', orderTimelineId],
    () => staffService.getOrderPaymentTimeline(orderTimelineId),
    { enabled: !!orderTimelineId }
  );

  const driverOptionsQuery = useQuery(
    ['deliveryReportDriversForCollections'],
    () => staffService.getDeliveryPersons({ per_page: 100 }),
    { enabled: recordCollectionOpen }
  );

  const recordCollectionStatementQuery = useQuery(
    ['deliveryReportRecordCollectionStatement', recordCollectionCustomerId],
    () => staffService.getCustomerCodStatement(recordCollectionCustomerId),
    { enabled: recordCollectionOpen && !!recordCollectionCustomerId }
  );

  const refreshReportQueries = () => {
    queryClient.invalidateQueries(reportQueryKey);
    if (selectedSessionId) {
      queryClient.invalidateQueries(['deliveryReportSession', selectedSessionId]);
    }
  };

  const verifyMutation = useMutation(
    ({ sessionId, payload }) => staffService.verifyCashReconciliationSession(sessionId, payload),
    {
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
    }
  );

  const resolveMutation = useMutation(
    ({ sessionId, payload }) => staffService.resolveCashReconciliationSession(sessionId, payload),
    {
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
    }
  );

  const recordCollectionMutation = useMutation(
    (payload) => staffService.recordCashCollection(payload),
    {
      onSuccess: () => {
        message.success(t('staff:cash_collection_recorded', 'Cash collection recorded'));
        setRecordCollectionOpen(false);
        setRecordCollectionCustomerId(null);
        setCustomerOptions([]);
        recordCollectionForm.resetFields();
        refreshReportQueries();
      },
      onError: (error) => {
        const backendMessage = error?.response?.data?.message;
        message.error(backendMessage || t('common:error_occurred'));
      },
    }
  );

  const payload = data?.data?.data || {};
  const summary = payload.summary || {};
  const report = payload.report || [];
  const sessions = payload.sessions || [];
  const sessionDetail = sessionDetailQuery.data?.data?.data || null;
  const customerStatement = customerStatementQuery.data?.data?.data || null;
  const orderTimeline = orderTimelineQuery.data?.data?.data || null;
  const recordCollectionStatement = recordCollectionStatementQuery.data?.data?.data || null;
  const deliveryDrivers = driverOptionsQuery.data?.data?.data?.items || [];

  const selectedSession = useMemo(
    () => sessionDetail || sessions.find((session) => session.id === selectedSessionId) || null,
    [sessionDetail, sessions, selectedSessionId]
  );

  const canReviewSession = (session) => ['open', 'submitted', 'overdue'].includes(session.status);
  const canResolveSession = (session) => ['mismatch', 'overdue'].includes(session.status);

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
          ? Math.max(0, (session.expected_cash ?? session.declared_cash ?? 0) - 1)
          : (session.declared_cash ?? session.expected_cash ?? 0),
      notes: session.verification_notes || '',
    });
    setVerifyOpen(true);
  };

  const openResolveModal = (session) => {
    setSelectedSessionId(session.id);
    resolveForm.setFieldsValue({
      verified_cash: session.verified_cash ?? session.declared_cash ?? session.expected_cash ?? 0,
      resolution_notes: session.resolution_notes || '',
    });
    setResolveOpen(true);
  };

  const handleCustomerSearch = async (searchValue) => {
    const query = (searchValue || '').trim();
    if (query.length < 2) {
      setCustomerOptions([]);
      return;
    }

    setCustomerSearchLoading(true);
    try {
      const response = await adminService.getUsers({
        search: query,
        role: 'customer',
        per_page: 20,
      });
      const items = response?.data?.items || [];
      setCustomerOptions(
        items.filter((item) => {
          const role = String(item?.role || '').toLowerCase();
          const userType = String(item?.user_type || '').toLowerCase();
          return role === 'customer' || (userType && userType !== 'staff');
        })
      );
    } catch (_error) {
      setCustomerOptions([]);
    } finally {
      setCustomerSearchLoading(false);
    }
  };

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
      title: t('staff:overdue_sessions', 'Overdue'),
      dataIndex: 'overdue_session_count',
      key: 'overdue_session_count',
    },
  ];

  const sessionColumns = [
    {
      title: t('staff:driver_name'),
      dataIndex: 'driver_name',
      key: 'driver_name',
    },
    {
      title: t('staff:business_date', 'Business Date'),
      dataIndex: 'business_date',
      key: 'business_date',
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
      title: t('staff:expected_cash', 'Expected Cash'),
      dataIndex: 'expected_cash',
      key: 'expected_cash',
      render: money,
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
        </Space>
      ),
    },
  ];

  const eventColumns = [
    {
      title: t('staff:occurred_at', 'Occurred'),
      dataIndex: 'occurred_at',
      key: 'occurred_at',
    },
    {
      title: t('staff:source', 'Source'),
      dataIndex: 'source',
      key: 'source',
      render: (value) => <Tag>{value}</Tag>,
    },
    {
      title: t('staff:amount', 'Amount'),
      dataIndex: 'amount',
      key: 'amount',
      render: money,
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
        </Space>
      ),
    },
  ];

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
              <Option value="submitted">{t('staff:submitted', 'Submitted')}</Option>
              <Option value="verified">{t('staff:verified', 'Verified')}</Option>
              <Option value="mismatch">{t('staff:mismatch', 'Mismatch')}</Option>
              <Option value="overdue">{t('staff:overdue', 'Overdue')}</Option>
              <Option value="resolved">{t('staff:resolved', 'Resolved')}</Option>
            </Select>
            <Button
              type={blockedOnly ? 'primary' : 'default'}
              icon={<WarningOutlined />}
              onClick={() => setBlockedOnly((value) => !value)}
            >
              {blockedOnly
                ? t('staff:showing_blocked_only', 'Blocked only')
                : t('staff:filter_blocked', 'Filter blocked')}
            </Button>
            <Button icon={<ReloadOutlined />} onClick={() => refetch()}>
              {t('common:refresh')}
            </Button>
            <Button type="primary" icon={<DollarOutlined />} onClick={() => setRecordCollectionOpen(true)}>
              {t('staff:record_cash_collection', 'Record Collection')}
            </Button>
          </Space>
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={12} md={6}>
          <Card>
            <Statistic
              title={t('staff:grand_total')}
              value={payload.grand_total_cash || 0}
              prefix={<DollarOutlined />}
              suffix="UZS"
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card>
            <Statistic
              title={t('staff:blocked_sessions', 'Blocked Sessions')}
              value={summary.blocked_session_count || 0}
              prefix={<WarningOutlined />}
              valueStyle={{ color: '#ff4d4f' }}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card>
            <Statistic
              title={t('staff:mismatch_sessions', 'Mismatches')}
              value={summary.mismatch_session_count || 0}
              prefix={<ExclamationCircleOutlined />}
              valueStyle={{ color: '#faad14' }}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} md={6}>
          <Card>
            <Statistic
              title={t('staff:overdue_sessions', 'Overdue')}
              value={summary.overdue_session_count || 0}
              prefix={<ClockCircleOutlined />}
              valueStyle={{ color: '#cf1322' }}
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
            'Approve accepts the reconciliation, Reject marks it as a mismatch and blocks COD work, and Resolve clears a previously mismatched or overdue session with audited notes.'
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
        width={1100}
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
              <Descriptions.Item label={t('staff:business_date', 'Business Date')}>{selectedSession.business_date}</Descriptions.Item>
              <Descriptions.Item label={t('staff:expected_cash', 'Expected Cash')}>{money(selectedSession.expected_cash)}</Descriptions.Item>
              <Descriptions.Item label={t('staff:declared_cash', 'Declared Cash')}>
                {selectedSession.declared_cash == null ? '—' : money(selectedSession.declared_cash)}
              </Descriptions.Item>
              <Descriptions.Item label={t('staff:verified_cash', 'Verified Cash')}>
                {selectedSession.verified_cash == null ? '—' : money(selectedSession.verified_cash)}
              </Descriptions.Item>
              <Descriptions.Item label={t('staff:declared_variance', 'Declared Variance')}>{money(selectedSession.declared_variance)}</Descriptions.Item>
              <Descriptions.Item label={t('staff:verified_variance', 'Verified Variance')}>{money(selectedSession.verified_variance)}</Descriptions.Item>
              <Descriptions.Item label={t('staff:block_reason', 'Block Reason')}>{selectedSession.block_reason || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('staff:event_count', 'Collection Events')}>{selectedSession.event_count || 0}</Descriptions.Item>
            </Descriptions>

            {selectedSession.blocked_from_cod ? (
              <Alert
                type="warning"
                showIcon
                style={{ marginTop: 16 }}
                message={t('staff:driver_blocked_from_cod', 'Driver is blocked from new COD work until this session is cleared.')}
              />
            ) : null}

            <Divider>{t('staff:collection_events', 'Collection Events')}</Divider>
            <Table
              columns={eventColumns}
              dataSource={selectedSession.events || []}
              rowKey="id"
              pagination={false}
              scroll={{ x: 900 }}
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
        confirmLoading={verifyMutation.isLoading}
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
            if (
              verifyMode === 'reject' &&
              selectedSession &&
              Number(values.verified_cash) === Number(selectedSession.expected_cash || 0)
            ) {
              message.error(
                t(
                  'staff:reject_requires_difference',
                  'Rejected reconciliations must use a verified cash amount that differs from expected cash.'
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
        confirmLoading={resolveMutation.isLoading}
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
            name="resolution_notes"
            label={t('staff:resolution_notes', 'Resolution Notes')}
            rules={[{ required: true, message: t('staff:resolution_notes_required', 'Resolution notes are required') }]}
          >
            <Input.TextArea rows={4} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t('staff:customer_statement', 'Customer COD Statement')}
        open={!!customerStatementId}
        onCancel={() => setCustomerStatementId(null)}
        footer={null}
        width={900}
      >
        {customerStatementQuery.isLoading ? (
          <Text>{t('common:loading', 'Loading...')}</Text>
        ) : customerStatement ? (
          <>
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
        open={!!orderTimelineId}
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
          setCustomerOptions([]);
          recordCollectionForm.resetFields();
        }}
        onOk={() => recordCollectionForm.submit()}
        confirmLoading={recordCollectionMutation.isLoading}
      >
        <Form
          form={recordCollectionForm}
          layout="vertical"
          initialValues={{ source: 'standalone_meeting' }}
          onFinish={(values) => {
            if (
              values.source !== 'admin_adjustment' &&
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
              filterOption={false}
              onSearch={handleCustomerSearch}
              loading={customerSearchLoading}
              onChange={(value) => setRecordCollectionCustomerId(value)}
              placeholder={t('staff:search_customer', 'Search customer by phone or name')}
            >
              {customerOptions.map((customer) => (
                <Option key={customer.id} value={customer.id}>
                  {customer.first_name} {customer.last_name} - {customer.phone}
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

          <Form.Item
            name="source"
            label={t('staff:collection_type', 'Collection Type')}
            rules={[{ required: true }]}
          >
            <Select
              onChange={(value) => {
                if (value === 'admin_adjustment') {
                  recordCollectionForm.setFieldsValue({ collector_user_id: undefined });
                }
              }}
            >
              <Option value="standalone_meeting">{t('staff:standalone_office_collection', 'Standalone / office collection')}</Option>
              <Option value="admin_adjustment">{t('staff:admin_correction', 'Admin correction')}</Option>
            </Select>
          </Form.Item>

          <Form.Item name="collector_user_id" label={t('staff:collector_driver', 'Collector Driver')}>
            <Select
              allowClear
              showSearch
              optionFilterProp="children"
              disabled={collectionSource === 'admin_adjustment'}
              placeholder={t('staff:collector_driver_optional', 'Optional driver attribution')}
            >
              {deliveryDrivers.map((driver) => (
                <Option key={driver.user_id || driver.id} value={driver.user_id || driver.id}>
                  {driver.full_name || driver.name} - {driver.phone}
                </Option>
              ))}
            </Select>
          </Form.Item>

          <Form.Item name="order_id" label={t('staff:target_order_optional', 'Target Order (optional)')}>
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
    </div>
  );
};

export default DeliveryReports;
