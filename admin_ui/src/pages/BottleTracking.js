import React, { useMemo, useRef, useState } from 'react';
import {
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  AuditOutlined,
  DollarOutlined,
  EditOutlined,
  ExclamationCircleOutlined,
  EyeOutlined,
  PlusOutlined,
  ReloadOutlined,
  SyncOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from 'react-query';

import adminService from '../services/adminService';
import { formatDate, formatDateTimeShort } from '../utils/dateUtils';

const { Text } = Typography;

const EVENT_TYPE_COLORS = {
  delivery: 'blue',
  return_on_delivery: 'green',
  standalone_collection: 'cyan',
  admin_adjustment: 'purple',
  fine_issued: 'red',
  fine_reversed: 'orange',
  fine_paid: 'green',
  initial_balance: 'geekblue',
};

const EVENT_TYPE_LABELS = {
  delivery: 'Delivery',
  return_on_delivery: 'Return (Delivery)',
  standalone_collection: 'Standalone Collection',
  admin_adjustment: 'Admin Adjustment',
  fine_issued: 'Fine Issued',
  fine_reversed: 'Fine Reversed',
  fine_paid: 'Fine Paid',
  initial_balance: 'Initial Balance',
};

const FINE_STATUS_COLORS = {
  pending: 'orange',
  invoiced: 'blue',
  paid: 'green',
  waived: 'default',
};

const formatCurrency = (amount) => {
  if (amount == null) return '—';
  return Number(amount).toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 0 });
};

// --- Dashboard Tab ---
const DashboardStats = ({ stats, loading }) => (
  <Row gutter={[16, 16]}>
    <Col xs={12} sm={6}>
      <Card>
        <Statistic
          title="Total Bottles Out"
          value={stats?.total_bottles_out ?? 0}
          prefix={<ExclamationCircleOutlined />}
        />
      </Card>
    </Col>
    <Col xs={12} sm={6}>
      <Card>
        <Statistic
          title="Customers with Balance"
          value={stats?.customers_with_balance ?? 0}
        />
      </Card>
    </Col>
    <Col xs={12} sm={6}>
      <Card>
        <Statistic
          title="Active Fines"
          value={stats?.active_fines ?? 0}
          prefix={<WarningOutlined />}
          valueStyle={stats?.active_fines > 0 ? { color: '#cf1322' } : undefined}
        />
      </Card>
    </Col>
    <Col xs={12} sm={6}>
      <Card>
        <Statistic
          title="Total Fine Amount"
          value={formatCurrency(stats?.total_fine_amount ?? 0)}
          prefix={<DollarOutlined />}
        />
      </Card>
    </Col>
  </Row>
);

// --- Reusable customer + address picker ---
const formatCustomerLabel = (user) => {
  const name = [user.first_name, user.last_name].filter(Boolean).join(' ').trim();
  const primary = name || user.company_name || user.email || `User #${user.id}`;
  const bits = [];
  if (user.phone) bits.push(user.phone);
  if (user.company_name && name) bits.push(user.company_name);
  return bits.length ? `${primary} — ${bits.join(' · ')}` : primary;
};

const CustomerAddressFields = ({ form, userFieldName = 'user_id', addressFieldName = 'address_id' }) => {
  const [searchTerm, setSearchTerm] = useState('');
  const debounceRef = useRef();

  const selectedUserId = Form.useWatch(userFieldName, form);

  const { data: usersData, isFetching: usersFetching } = useQuery(
    ['bottle-customer-search', searchTerm],
    () => adminService.getUsers({ search: searchTerm, per_page: 20 }),
    { enabled: searchTerm.length >= 2, keepPreviousData: true }
  );

  const { data: selectedUserData } = useQuery(
    ['bottle-customer-details', selectedUserId],
    () => adminService.getUserDetails(selectedUserId),
    { enabled: Boolean(selectedUserId) }
  );

  const { data: addressesData, isFetching: addressesFetching } = useQuery(
    ['bottle-customer-addresses', selectedUserId],
    () => adminService.getUserAddresses(selectedUserId),
    { enabled: Boolean(selectedUserId) }
  );

  const users = usersData?.data?.users || usersData?.data?.items || usersData?.data || [];
  const selectedUser = selectedUserData?.data?.user || selectedUserData?.data || null;

  const userOptions = useMemo(() => {
    const options = users.map((u) => ({ value: u.id, label: formatCustomerLabel(u), user: u }));
    if (selectedUser && !options.find((o) => o.value === selectedUser.id)) {
      options.unshift({ value: selectedUser.id, label: formatCustomerLabel(selectedUser), user: selectedUser });
    }
    return options;
  }, [users, selectedUser]);

  const addresses = addressesData?.data?.addresses || addressesData?.data?.items || addressesData?.data || [];
  const addressOptions = addresses.map((a) => ({
    value: a.id,
    label: a.title || a.label || a.address || `Address #${a.id}`,
  }));

  const handleSearch = (val) => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => setSearchTerm(val.trim()), 300);
  };

  return (
    <>
      <Form.Item name={userFieldName} label="Customer" rules={[{ required: true, message: 'Select a customer' }]}>
        <Select
          showSearch
          placeholder="Search by phone, name, or company"
          filterOption={false}
          onSearch={handleSearch}
          loading={usersFetching}
          options={userOptions}
          onChange={() => form.setFieldValue(addressFieldName, undefined)}
          notFoundContent={searchTerm.length < 2 ? 'Type at least 2 characters' : (usersFetching ? 'Searching…' : 'No matches')}
        />
      </Form.Item>
      <Form.Item name={addressFieldName} label="Address" rules={[{ required: true, message: 'Select an address' }]}>
        <Select
          placeholder={selectedUserId ? 'Select an address' : 'Select customer first'}
          disabled={!selectedUserId}
          loading={addressesFetching}
          options={addressOptions}
          notFoundContent={!selectedUserId ? 'Select customer first' : 'No addresses'}
        />
      </Form.Item>
    </>
  );
};

// --- Main Component ---
const BottleTracking = () => {
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState('balances');

  // Balances state
  const [balanceSearch, setBalanceSearch] = useState('');
  const [balanceMinBalance, setBalanceMinBalance] = useState();
  const [balancePagination, setBalancePagination] = useState({ page: 1, per_page: 20 });

  // Ledger state
  const [ledgerEventType, setLedgerEventType] = useState();
  const [ledgerPagination, setLedgerPagination] = useState({ page: 1, per_page: 20 });

  // Fines state
  const [fineStatus, setFineStatus] = useState();
  const [finePagination, setFinePagination] = useState({ page: 1, per_page: 20 });

  // Sessions state
  const [sessionPagination, setSessionPagination] = useState({ page: 1, per_page: 20 });
  const [sessionStatusFilter, setSessionStatusFilter] = useState();
  const [sessionOnlyDiscrepancies, setSessionOnlyDiscrepancies] = useState(false);
  const [sessionDetailOpen, setSessionDetailOpen] = useState(false);
  const [sessionDetailTarget, setSessionDetailTarget] = useState(null);
  const [forceCloseOpen, setForceCloseOpen] = useState(false);
  const [forceCloseTarget, setForceCloseTarget] = useState(null);
  const [forceCloseForm] = Form.useForm();

  // Transfers state
  const [transferPagination, setTransferPagination] = useState({ page: 1, per_page: 20 });
  const [transferStatusFilter, setTransferStatusFilter] = useState();
  const [resolveOpen, setResolveOpen] = useState(false);
  const [resolveTarget, setResolveTarget] = useState(null);
  const [resolveForm] = Form.useForm();

  // Modals
  const [adjustmentOpen, setAdjustmentOpen] = useState(false);
  const [initialBalanceOpen, setInitialBalanceOpen] = useState(false);
  const [fineCreateOpen, setFineCreateOpen] = useState(false);
  const [ledgerDrawerOpen, setLedgerDrawerOpen] = useState(false);
  const [ledgerDrawerTarget, setLedgerDrawerTarget] = useState(null);

  // Forms
  const [adjustmentForm] = Form.useForm();
  const [initialBalanceForm] = Form.useForm();
  const [fineForm] = Form.useForm();

  // --- Queries ---
  const { data: dashboardData, isLoading: dashboardLoading } = useQuery(
    ['bottle-dashboard'],
    () => adminService.getBottleDashboard(),
    { staleTime: 30_000 }
  );

  const balanceFilters = useMemo(
    () => ({
      page: balancePagination.page,
      per_page: balancePagination.per_page,
      search: balanceSearch || undefined,
      min_balance: balanceMinBalance || undefined,
    }),
    [balancePagination, balanceSearch, balanceMinBalance]
  );

  const { data: balancesData, isLoading: balancesLoading } = useQuery(
    ['bottle-balances', balanceFilters],
    () => adminService.getBottleBalances(balanceFilters),
    { keepPreviousData: true }
  );

  const ledgerFilters = useMemo(
    () => ({
      page: ledgerPagination.page,
      per_page: ledgerPagination.per_page,
      event_type: ledgerEventType || undefined,
    }),
    [ledgerPagination, ledgerEventType]
  );

  const { data: ledgerData, isLoading: ledgerLoading } = useQuery(
    ['bottle-ledger', ledgerFilters],
    () => adminService.getBottleLedger(ledgerFilters),
    { keepPreviousData: true, enabled: activeTab === 'ledger' }
  );

  const fineFilters = useMemo(
    () => ({
      page: finePagination.page,
      per_page: finePagination.per_page,
      status: fineStatus || undefined,
    }),
    [finePagination, fineStatus]
  );

  const { data: finesData, isLoading: finesLoading } = useQuery(
    ['bottle-fines', fineFilters],
    () => adminService.getBottleFines(fineFilters),
    { keepPreviousData: true, enabled: activeTab === 'fines' }
  );

  const sessionFilters = useMemo(
    () => ({
      page: sessionPagination.page,
      per_page: sessionPagination.per_page,
      status: sessionStatusFilter || undefined,
      only_discrepancies: sessionOnlyDiscrepancies || undefined,
    }),
    [sessionPagination, sessionStatusFilter, sessionOnlyDiscrepancies]
  );

  const { data: sessionsData, isLoading: sessionsLoading } = useQuery(
    ['bottle-sessions', sessionFilters],
    () => adminService.getBottleSessions(sessionFilters),
    { keepPreviousData: true, enabled: activeTab === 'sessions' }
  );

  const { data: sessionDetailData, isLoading: sessionDetailLoading } = useQuery(
    ['bottle-session-detail', sessionDetailTarget],
    () => adminService.getBottleSession(sessionDetailTarget),
    { enabled: Boolean(sessionDetailTarget) }
  );

  const transferFilters = useMemo(
    () => ({
      page: transferPagination.page,
      per_page: transferPagination.per_page,
      status: transferStatusFilter || undefined,
    }),
    [transferPagination, transferStatusFilter]
  );

  const { data: transfersData, isLoading: transfersLoading } = useQuery(
    ['bottle-transfers', transferFilters],
    () => adminService.getBottleTransfers(transferFilters),
    { keepPreviousData: true, enabled: activeTab === 'transfers' }
  );

  // Ledger drawer query
  const { data: addressLedgerData, isLoading: addressLedgerLoading } = useQuery(
    ['bottle-address-ledger', ledgerDrawerTarget?.user_id, ledgerDrawerTarget?.address_id],
    () => adminService.getBottleLedgerForAddress(
      ledgerDrawerTarget.user_id,
      ledgerDrawerTarget.address_id,
      { per_page: 50 }
    ),
    { enabled: Boolean(ledgerDrawerTarget) }
  );

  const dashboard = dashboardData?.data || {};
  const balances = balancesData?.data?.items || balancesData?.data || [];
  const balancesTotal = balancesData?.data?.total || balances.length;
  const ledgerEntries = ledgerData?.data?.items || ledgerData?.data || [];
  const ledgerTotal = ledgerData?.data?.total || ledgerEntries.length;
  const fines = finesData?.data?.items || finesData?.data || [];
  const finesTotal = finesData?.data?.total || fines.length;
  const sessions = sessionsData?.data?.items || sessionsData?.data || [];
  const sessionsTotal = sessionsData?.data?.total || sessions.length;
  const sessionDetail = sessionDetailData?.data || null;

  const transfers = transfersData?.data?.items || transfersData?.data || [];
  const transfersTotal = transfersData?.data?.total || transfers.length;

  const refreshAll = () => {
    queryClient.invalidateQueries(['bottle-dashboard']);
    queryClient.invalidateQueries(['bottle-balances']);
    queryClient.invalidateQueries(['bottle-ledger']);
    queryClient.invalidateQueries(['bottle-fines']);
    queryClient.invalidateQueries(['bottle-sessions']);
    queryClient.invalidateQueries(['bottle-transfers']);
  };

  // --- Mutations ---
  const adjustmentMutation = useMutation(
    (data) => adminService.createBottleAdjustment(data),
    {
      onSuccess: () => {
        message.success('Balance adjusted');
        setAdjustmentOpen(false);
        adjustmentForm.resetFields();
        refreshAll();
      },
      onError: (err) => message.error(err?.response?.data?.error || 'Failed to adjust balance'),
    }
  );

  const initialBalanceMutation = useMutation(
    (data) => adminService.setBottleInitialBalance(data),
    {
      onSuccess: () => {
        message.success('Initial balance set');
        setInitialBalanceOpen(false);
        initialBalanceForm.resetFields();
        refreshAll();
      },
      onError: (err) => message.error(err?.response?.data?.error || 'Failed to set initial balance'),
    }
  );

  const fineCreateMutation = useMutation(
    (data) => adminService.createBottleFine(data),
    {
      onSuccess: () => {
        message.success('Fine created');
        setFineCreateOpen(false);
        fineForm.resetFields();
        refreshAll();
      },
      onError: (err) => message.error(err?.response?.data?.error || 'Failed to create fine'),
    }
  );

  const fineUpdateMutation = useMutation(
    ({ fineId, data }) => adminService.updateBottleFine(fineId, data),
    {
      onSuccess: () => {
        message.success('Fine updated');
        refreshAll();
      },
      onError: (err) => message.error(err?.response?.data?.error || 'Failed to update fine'),
    }
  );

  const reconcileMutation = useMutation(
    ({ userId, addressId }) => adminService.reconcileBottleBalance(userId, addressId),
    {
      onSuccess: (res) => {
        const diff = res?.data?.difference;
        if (diff && diff !== 0) {
          message.warning(`Reconciled — balance corrected by ${diff}`);
        } else {
          message.success('Balance is consistent');
        }
        refreshAll();
      },
      onError: (err) => message.error(err?.response?.data?.error || 'Reconciliation failed'),
    }
  );

  const forceCloseMutation = useMutation(
    ({ sessionId, data }) => adminService.forceCloseBottleSession(sessionId, data),
    {
      onSuccess: () => {
        message.success('Session force-closed');
        setForceCloseOpen(false);
        setForceCloseTarget(null);
        forceCloseForm.resetFields();
        queryClient.invalidateQueries(['bottle-sessions']);
      },
      onError: (err) => message.error(err?.response?.data?.error || 'Failed to force-close session'),
    }
  );

  const resolveTransferMutation = useMutation(
    ({ transferId, data }) => adminService.resolveBottleTransferDispute(transferId, data),
    {
      onSuccess: () => {
        message.success('Transfer dispute resolved');
        setResolveOpen(false);
        setResolveTarget(null);
        resolveForm.resetFields();
        queryClient.invalidateQueries(['bottle-transfers']);
        queryClient.invalidateQueries(['bottle-sessions']);
      },
      onError: (err) => message.error(err?.response?.data?.error || 'Failed to resolve dispute'),
    }
  );

  // --- Balance columns ---
  const balanceColumns = [
    {
      title: 'Customer',
      key: 'customer',
      render: (_, record) => (
        <Space direction="vertical" size={0}>
          <Text strong>{record.customer_name || record.user_name || `User #${record.user_id}`}</Text>
          <Text type="secondary">{record.customer_phone || record.user_phone || ''}</Text>
        </Space>
      ),
    },
    {
      title: 'Address',
      key: 'address',
      render: (_, record) => record.address_title || record.address_label || `Address #${record.address_id}`,
    },
    {
      title: 'Balance',
      dataIndex: 'balance',
      key: 'balance',
      sorter: (a, b) => (a.balance || 0) - (b.balance || 0),
      render: (val) => {
        const num = Number(val) || 0;
        return (
          <Text strong style={num > 0 ? { color: '#cf1322' } : undefined}>
            {num}
          </Text>
        );
      },
    },
    {
      title: 'Last Delivery',
      dataIndex: 'last_delivery_at',
      key: 'last_delivery_at',
      render: (val) => (val ? formatDateTimeShort(val) : '—'),
    },
    {
      title: 'Last Return',
      dataIndex: 'last_return_at',
      key: 'last_return_at',
      render: (val) => (val ? formatDateTimeShort(val) : '—'),
    },
    {
      title: 'Actions',
      key: 'actions',
      render: (_, record) => (
        <Space>
          <Button
            size="small"
            icon={<EyeOutlined />}
            onClick={() => {
              setLedgerDrawerTarget({ user_id: record.user_id, address_id: record.address_id });
              setLedgerDrawerOpen(true);
            }}
          >
            Ledger
          </Button>
          <Button
            size="small"
            icon={<EditOutlined />}
            onClick={() => {
              adjustmentForm.setFieldsValue({
                user_id: record.user_id,
                address_id: record.address_id,
              });
              setAdjustmentOpen(true);
            }}
          >
            Adjust
          </Button>
          <Button
            size="small"
            icon={<SyncOutlined />}
            loading={reconcileMutation.isLoading}
            onClick={() => reconcileMutation.mutate({ userId: record.user_id, addressId: record.address_id })}
          >
            Reconcile
          </Button>
        </Space>
      ),
    },
  ];

  // --- Ledger columns ---
  const ledgerColumns = [
    {
      title: 'Date',
      dataIndex: 'occurred_at',
      key: 'occurred_at',
      render: (val) => (val ? formatDateTimeShort(val) : '—'),
      width: 160,
    },
    {
      title: 'Customer',
      key: 'customer',
      render: (_, record) => record.customer_name || record.user_name || `User #${record.user_id}`,
    },
    {
      title: 'Address',
      key: 'address',
      render: (_, record) => record.address_title || `Address #${record.address_id}`,
    },
    {
      title: 'Event',
      dataIndex: 'event_type',
      key: 'event_type',
      render: (val) => (
        <Tag color={EVENT_TYPE_COLORS[val] || 'default'}>
          {EVENT_TYPE_LABELS[val] || val}
        </Tag>
      ),
    },
    {
      title: 'Quantity',
      dataIndex: 'quantity',
      key: 'quantity',
      render: (val) => {
        const num = Number(val) || 0;
        const color = num > 0 ? '#cf1322' : num < 0 ? '#389e0d' : undefined;
        return <Text strong style={{ color }}>{num > 0 ? `+${num}` : num}</Text>;
      },
    },
    {
      title: 'Balance After',
      dataIndex: 'balance_after',
      key: 'balance_after',
      render: (val) => Number(val) || 0,
    },
    {
      title: 'Actor',
      key: 'actor',
      render: (_, record) => record.actor_name || record.actor_user_name || '—',
    },
    {
      title: 'Notes',
      dataIndex: 'notes',
      key: 'notes',
      ellipsis: true,
    },
  ];

  // --- Fine columns ---
  const fineColumns = [
    {
      title: 'Customer',
      key: 'customer',
      render: (_, record) => record.customer_name || record.user_name || `User #${record.user_id}`,
    },
    {
      title: 'Quantity',
      dataIndex: 'quantity',
      key: 'quantity',
    },
    {
      title: 'Fine Amount',
      dataIndex: 'fine_amount',
      key: 'fine_amount',
      render: (val) => formatCurrency(val),
    },
    {
      title: 'Status',
      dataIndex: 'status',
      key: 'status',
      render: (val) => (
        <Tag color={FINE_STATUS_COLORS[val] || 'default'}>
          {(val || '').toUpperCase()}
        </Tag>
      ),
    },
    {
      title: 'Issued By',
      key: 'issued_by',
      render: (_, record) => record.issued_by_name || '—',
    },
    {
      title: 'Issued At',
      dataIndex: 'issued_at',
      key: 'issued_at',
      render: (val) => (val ? formatDate(val) : '—'),
    },
    {
      title: 'Notes',
      dataIndex: 'notes',
      key: 'notes',
      ellipsis: true,
    },
    {
      title: 'Actions',
      key: 'actions',
      render: (_, record) => {
        if (record.status === 'paid' || record.status === 'waived') return null;
        return (
          <Space>
            <Button
              size="small"
              type="primary"
              onClick={() => fineUpdateMutation.mutate({ fineId: record.id, data: { action: 'mark_paid' } })}
              loading={fineUpdateMutation.isLoading}
            >
              Mark Paid
            </Button>
            <Button
              size="small"
              danger
              onClick={() => fineUpdateMutation.mutate({ fineId: record.id, data: { action: 'waive', notes: 'Waived by admin' } })}
              loading={fineUpdateMutation.isLoading}
            >
              Waive
            </Button>
          </Space>
        );
      },
    },
  ];

  // --- Address ledger columns (drawer) ---
  const addressLedgerColumns = [
    {
      title: 'Date',
      dataIndex: 'occurred_at',
      key: 'occurred_at',
      render: (val) => (val ? formatDateTimeShort(val) : '—'),
    },
    {
      title: 'Event',
      dataIndex: 'event_type',
      key: 'event_type',
      render: (val) => (
        <Tag color={EVENT_TYPE_COLORS[val] || 'default'}>
          {EVENT_TYPE_LABELS[val] || val}
        </Tag>
      ),
    },
    {
      title: 'Qty',
      dataIndex: 'quantity',
      key: 'quantity',
      render: (val) => {
        const num = Number(val) || 0;
        const color = num > 0 ? '#cf1322' : num < 0 ? '#389e0d' : undefined;
        return <Text strong style={{ color }}>{num > 0 ? `+${num}` : num}</Text>;
      },
    },
    {
      title: 'Balance After',
      dataIndex: 'balance_after',
      key: 'balance_after',
    },
    { title: 'Notes', dataIndex: 'notes', key: 'notes', ellipsis: true },
  ];

  const addressLedgerEntries = addressLedgerData?.data?.items || addressLedgerData?.data || [];

  // --- Tab items ---
  const tabItems = [
    {
      key: 'balances',
      label: 'Balances',
      children: (
        <>
          <Space style={{ marginBottom: 16 }} wrap>
            <Input.Search
              placeholder="Search customer..."
              allowClear
              style={{ width: 250 }}
              onSearch={(val) => {
                setBalanceSearch(val);
                setBalancePagination((p) => ({ ...p, page: 1 }));
              }}
            />
            <InputNumber
              placeholder="Min balance"
              min={1}
              style={{ width: 130 }}
              onChange={(val) => {
                setBalanceMinBalance(val);
                setBalancePagination((p) => ({ ...p, page: 1 }));
              }}
            />
            <Button icon={<PlusOutlined />} onClick={() => setInitialBalanceOpen(true)}>
              Set Initial Balance
            </Button>
            <Button icon={<EditOutlined />} onClick={() => setAdjustmentOpen(true)}>
              Adjust Balance
            </Button>
          </Space>
          <Table
            columns={balanceColumns}
            dataSource={balances}
            rowKey={(r) => r.id || `${r.user_id}_${r.address_id}`}
            loading={balancesLoading}
            pagination={{
              current: balancePagination.page,
              pageSize: balancePagination.per_page,
              total: balancesTotal,
              showSizeChanger: true,
              onChange: (page, per_page) => setBalancePagination({ page, per_page }),
            }}
            size="middle"
          />
        </>
      ),
    },
    {
      key: 'ledger',
      label: 'Ledger',
      children: (
        <>
          <Space style={{ marginBottom: 16 }} wrap>
            <Select
              placeholder="Event type"
              allowClear
              style={{ width: 200 }}
              onChange={(val) => {
                setLedgerEventType(val);
                setLedgerPagination((p) => ({ ...p, page: 1 }));
              }}
              options={Object.entries(EVENT_TYPE_LABELS).map(([value, label]) => ({ value, label }))}
            />
          </Space>
          <Table
            columns={ledgerColumns}
            dataSource={ledgerEntries}
            rowKey={(r) => r.id}
            loading={ledgerLoading}
            pagination={{
              current: ledgerPagination.page,
              pageSize: ledgerPagination.per_page,
              total: ledgerTotal,
              showSizeChanger: true,
              onChange: (page, per_page) => setLedgerPagination({ page, per_page }),
            }}
            size="middle"
          />
        </>
      ),
    },
    {
      key: 'fines',
      label: 'Fines',
      children: (
        <>
          <Space style={{ marginBottom: 16 }} wrap>
            <Select
              placeholder="Status"
              allowClear
              style={{ width: 150 }}
              onChange={(val) => {
                setFineStatus(val);
                setFinePagination((p) => ({ ...p, page: 1 }));
              }}
              options={[
                { value: 'pending', label: 'Pending' },
                { value: 'invoiced', label: 'Invoiced' },
                { value: 'paid', label: 'Paid' },
                { value: 'waived', label: 'Waived' },
              ]}
            />
            <Button icon={<PlusOutlined />} onClick={() => setFineCreateOpen(true)}>
              Create Fine
            </Button>
          </Space>
          <Table
            columns={fineColumns}
            dataSource={fines}
            rowKey={(r) => r.id}
            loading={finesLoading}
            pagination={{
              current: finePagination.page,
              pageSize: finePagination.per_page,
              total: finesTotal,
              showSizeChanger: true,
              onChange: (page, per_page) => setFinePagination({ page, per_page }),
            }}
            size="middle"
          />
        </>
      ),
    },
    {
      key: 'sessions',
      label: 'Driver Sessions',
      children: (
        <>
          <Space wrap style={{ marginBottom: 12 }}>
            <Select
              allowClear
              placeholder="Status"
              style={{ width: 140 }}
              value={sessionStatusFilter}
              onChange={setSessionStatusFilter}
              options={[
                { value: 'open', label: 'Open' },
                { value: 'closed', label: 'Closed' },
                { value: 'force_closed', label: 'Force Closed' },
                { value: 'cancelled', label: 'Cancelled' },
              ]}
            />
            <Button
              type={sessionOnlyDiscrepancies ? 'primary' : 'default'}
              icon={<WarningOutlined />}
              onClick={() => setSessionOnlyDiscrepancies((v) => !v)}
            >
              Discrepancies only
            </Button>
          </Space>
          <Table
            columns={[
              { title: 'Driver', dataIndex: 'driver_name', key: 'driver_name', render: (v, r) => v || r.driver_user_id },
              { title: 'Started', dataIndex: 'started_at', key: 'started_at', render: (v) => v ? formatDateTimeShort(v) : '—' },
              { title: 'Closed', dataIndex: 'closed_at', key: 'closed_at', render: (v) => v ? formatDateTimeShort(v) : '—' },
              { title: 'Loaded', dataIndex: 'bottles_loaded', key: 'bottles_loaded', align: 'right' },
              { title: 'Delivered', dataIndex: 'bottles_delivered', key: 'bottles_delivered', align: 'right' },
              { title: 'Collected', dataIndex: 'bottles_collected_from_customers', key: 'bottles_collected_from_customers', align: 'right' },
              { title: 'On Truck', dataIndex: 'current_inventory', key: 'current_inventory', align: 'right' },
              { title: 'Returned', dataIndex: 'bottles_returned_to_warehouse', key: 'bottles_returned_to_warehouse', align: 'right', render: (v) => v ?? '—' },
              {
                title: 'Discrepancy', dataIndex: 'discrepancy', key: 'discrepancy', align: 'right',
                render: (v) => {
                  if (v == null) return '—';
                  return <Tag color={v === 0 ? 'green' : 'red'}>{v === 0 ? '✓ 0' : v}</Tag>;
                },
              },
              {
                title: 'Status', dataIndex: 'status', key: 'status',
                render: (v) => {
                  const colors = { open: 'blue', closed: 'green', force_closed: 'red', cancelled: 'default' };
                  return <Tag color={colors[v] || 'default'}>{(v || '').toUpperCase()}</Tag>;
                },
              },
              {
                title: 'Actions', key: 'actions',
                render: (_, record) => (
                  <Space>
                    <Button size="small" icon={<EyeOutlined />} onClick={() => { setSessionDetailTarget(record.id); setSessionDetailOpen(true); }}>
                      Detail
                    </Button>
                    {record.status === 'open' && (
                      <Button size="small" danger icon={<ExclamationCircleOutlined />} onClick={() => { setForceCloseTarget(record); setForceCloseOpen(true); }}>
                        Force Close
                      </Button>
                    )}
                  </Space>
                ),
              },
            ]}
            dataSource={sessions}
            rowKey="id"
            loading={sessionsLoading}
            rowClassName={(r) => r.discrepancy && r.discrepancy !== 0 ? 'ant-table-row-danger' : ''}
            pagination={{
              current: sessionPagination.page,
              pageSize: sessionPagination.per_page,
              total: sessionsTotal,
              showSizeChanger: true,
              onChange: (page, per_page) => setSessionPagination({ page, per_page }),
            }}
            size="middle"
          />
        </>
      ),
    },
    {
      key: 'transfers',
      label: 'Bottle Transfers',
      children: (
        <>
          <Space wrap style={{ marginBottom: 12 }}>
            <Select
              allowClear
              placeholder="Status"
              style={{ width: 150 }}
              value={transferStatusFilter}
              onChange={setTransferStatusFilter}
              options={[
                { value: 'pending', label: 'Pending' },
                { value: 'confirmed', label: 'Confirmed' },
                { value: 'disputed', label: 'Disputed' },
                { value: 'resolved', label: 'Resolved' },
              ]}
            />
          </Space>
          <Table
            columns={[
              { title: 'Sender', dataIndex: 'sender_name', key: 'sender_name', render: (v, r) => v || r.sender_driver_id },
              { title: 'Receiver', dataIndex: 'receiver_name', key: 'receiver_name', render: (v, r) => v || r.receiver_driver_id },
              { title: 'Declared', dataIndex: 'declared_quantity', key: 'declared_quantity', align: 'right' },
              { title: 'Confirmed', dataIndex: 'confirmed_quantity', key: 'confirmed_quantity', align: 'right', render: (v) => v ?? '—' },
              { title: 'Sent At', dataIndex: 'sent_at', key: 'sent_at', render: (v) => v ? formatDateTimeShort(v) : '—' },
              {
                title: 'Status', dataIndex: 'status', key: 'status',
                render: (v) => {
                  const colors = { pending: 'blue', confirmed: 'green', disputed: 'red', resolved: 'default' };
                  return <Tag color={colors[v] || 'default'}>{(v || '').toUpperCase()}</Tag>;
                },
              },
              {
                title: 'Actions', key: 'actions',
                render: (_, record) => record.status === 'disputed' ? (
                  <Button size="small" icon={<EditOutlined />} onClick={() => { setResolveTarget(record); setResolveOpen(true); }}>
                    Resolve
                  </Button>
                ) : null,
              },
            ]}
            dataSource={transfers}
            rowKey="id"
            loading={transfersLoading}
            pagination={{
              current: transferPagination.page,
              pageSize: transferPagination.per_page,
              total: transfersTotal,
              showSizeChanger: true,
              onChange: (page, per_page) => setTransferPagination({ page, per_page }),
            }}
            size="middle"
          />
        </>
      ),
    },
  ];

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <Typography.Title level={3} style={{ margin: 0 }}>
          Bottle Tracking
        </Typography.Title>
        <Button icon={<ReloadOutlined />} onClick={refreshAll}>
          Refresh
        </Button>
      </div>

      <DashboardStats stats={dashboard} loading={dashboardLoading} />

      <Card style={{ marginTop: 16 }}>
        <Tabs activeKey={activeTab} onChange={setActiveTab} items={tabItems} />
      </Card>

      {/* Adjustment Modal */}
      <Modal
        title="Adjust Bottle Balance"
        open={adjustmentOpen}
        onCancel={() => { setAdjustmentOpen(false); adjustmentForm.resetFields(); }}
        onOk={() => adjustmentForm.submit()}
        confirmLoading={adjustmentMutation.isLoading}
      >
        <Form
          form={adjustmentForm}
          layout="vertical"
          onFinish={(values) => adjustmentMutation.mutate(values)}
        >
          <CustomerAddressFields form={adjustmentForm} />
          <Form.Item
            name="adjustment"
            label="Adjustment (positive = add bottles to customer, negative = remove)"
            rules={[{ required: true }]}
          >
            <InputNumber style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="notes" label="Notes" rules={[{ required: true }]}>
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      {/* Initial Balance Modal */}
      <Modal
        title="Set Initial Bottle Balance"
        open={initialBalanceOpen}
        onCancel={() => { setInitialBalanceOpen(false); initialBalanceForm.resetFields(); }}
        onOk={() => initialBalanceForm.submit()}
        confirmLoading={initialBalanceMutation.isLoading}
      >
        <Form
          form={initialBalanceForm}
          layout="vertical"
          onFinish={(values) => initialBalanceMutation.mutate(values)}
        >
          <CustomerAddressFields form={initialBalanceForm} />
          <Form.Item name="quantity" label="Bottle Quantity" rules={[{ required: true }]}>
            <InputNumber style={{ width: '100%' }} min={0} />
          </Form.Item>
          <Form.Item name="notes" label="Notes">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      {/* Create Fine Modal */}
      <Modal
        title="Create Bottle Fine"
        open={fineCreateOpen}
        onCancel={() => { setFineCreateOpen(false); fineForm.resetFields(); }}
        onOk={() => fineForm.submit()}
        confirmLoading={fineCreateMutation.isLoading}
      >
        <Form
          form={fineForm}
          layout="vertical"
          onFinish={(values) => fineCreateMutation.mutate(values)}
        >
          <CustomerAddressFields form={fineForm} />
          <Form.Item name="quantity" label="Bottles to Fine For" rules={[{ required: true }]}>
            <InputNumber style={{ width: '100%' }} min={1} />
          </Form.Item>
          <Form.Item name="fine_amount" label="Fine Amount" rules={[{ required: true }]}>
            <InputNumber style={{ width: '100%' }} min={0} />
          </Form.Item>
          <Form.Item name="notes" label="Notes">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      {/* Address Ledger Drawer */}
      <Drawer
        title="Address Bottle Ledger"
        open={ledgerDrawerOpen}
        onClose={() => { setLedgerDrawerOpen(false); setLedgerDrawerTarget(null); }}
        width={700}
      >
        {ledgerDrawerTarget && (
          <Descriptions column={2} size="small" style={{ marginBottom: 16 }}>
            <Descriptions.Item label="User ID">{ledgerDrawerTarget.user_id}</Descriptions.Item>
            <Descriptions.Item label="Address ID">{ledgerDrawerTarget.address_id}</Descriptions.Item>
          </Descriptions>
        )}
        <Table
          columns={addressLedgerColumns}
          dataSource={addressLedgerEntries}
          rowKey={(r) => r.id}
          loading={addressLedgerLoading}
          pagination={false}
          size="small"
        />
      </Drawer>

      {/* Session Detail Drawer */}
      <Drawer
        title="Session Detail"
        open={sessionDetailOpen}
        onClose={() => { setSessionDetailOpen(false); setSessionDetailTarget(null); }}
        width={600}
      >
        {sessionDetail && (
          <>
            <Descriptions column={2} size="small" bordered style={{ marginBottom: 16 }}>
              <Descriptions.Item label="Driver">{sessionDetail.driver_name || sessionDetail.driver_user_id}</Descriptions.Item>
              <Descriptions.Item label="Status">
                <Tag color={{ open: 'blue', closed: 'green', force_closed: 'red', cancelled: 'default' }[sessionDetail.status] || 'default'}>
                  {(sessionDetail.status || '').toUpperCase()}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="Started">{sessionDetail.started_at ? formatDateTimeShort(sessionDetail.started_at) : '—'}</Descriptions.Item>
              <Descriptions.Item label="Closed">{sessionDetail.closed_at ? formatDateTimeShort(sessionDetail.closed_at) : '—'}</Descriptions.Item>
              <Descriptions.Item label="Loaded">{sessionDetail.bottles_loaded}</Descriptions.Item>
              <Descriptions.Item label="Delivered">{sessionDetail.bottles_delivered}</Descriptions.Item>
              <Descriptions.Item label="Collected">{sessionDetail.bottles_collected_from_customers}</Descriptions.Item>
              <Descriptions.Item label="Transferred Out">{sessionDetail.bottles_transferred_out}</Descriptions.Item>
              <Descriptions.Item label="Transferred In">{sessionDetail.bottles_transferred_in}</Descriptions.Item>
              <Descriptions.Item label="Returned to WH">{sessionDetail.bottles_returned_to_warehouse ?? '—'}</Descriptions.Item>
              <Descriptions.Item label="Discrepancy">
                {sessionDetail.discrepancy != null
                  ? <Tag color={sessionDetail.discrepancy === 0 ? 'green' : 'red'}>{sessionDetail.discrepancy}</Tag>
                  : '—'}
              </Descriptions.Item>
              {sessionDetail.force_close_reason && (
                <Descriptions.Item label="Force Close Reason" span={2}>{sessionDetail.force_close_reason}</Descriptions.Item>
              )}
            </Descriptions>
            {sessionDetail.members?.length > 0 && (
              <>
                <Text strong style={{ display: 'block', marginBottom: 8 }}>
                  Co-Drivers ({sessionDetail.members.length})
                </Text>
                <Table
                  style={{ marginBottom: 16 }}
                  size="small"
                  pagination={false}
                  dataSource={sessionDetail.members}
                  rowKey={(r) => r.membership_id || r.member_driver_id}
                  columns={[
                    { title: 'Driver', dataIndex: 'member_name', render: (v, r) => v || `Driver #${r.member_driver_id}` },
                    {
                      title: 'Status',
                      dataIndex: 'status',
                      render: (v) => (
                        <Tag color={{ active: 'blue', left: 'orange', revoked: 'red' }[v] || 'default'}>
                          {(v || '').toUpperCase()}
                        </Tag>
                      ),
                    },
                    { title: 'Joined', dataIndex: 'joined_at', render: (v) => v ? formatDateTimeShort(v) : '—' },
                    { title: 'Left', dataIndex: 'left_at', render: (v) => v ? formatDateTimeShort(v) : '—' },
                  ]}
                />
              </>
            )}
            {sessionDetail.orders?.length > 0 && (
              <>
                <Text strong>Bound Orders ({sessionDetail.orders.length})</Text>
                <Table
                  style={{ marginTop: 8 }}
                  size="small"
                  pagination={false}
                  dataSource={sessionDetail.orders}
                  rowKey="order_id"
                  columns={[
                    { title: 'Order #', dataIndex: 'order_number', render: (v) => v ?? '—' },
                    { title: 'Customer', dataIndex: 'customer_name', render: (v) => v ?? '—' },
                    {
                      title: 'Items',
                      dataIndex: 'items',
                      render: (items) =>
                        items?.length
                          ? items.map((i) => `${i.product_name} ×${i.quantity}`).join(', ')
                          : '—',
                    },
                    { title: 'Total', dataIndex: 'total_amount', render: (v) => v != null ? formatCurrency(v) : '—' },
                    { title: 'Status', dataIndex: 'status', render: (v) => v ? <Tag>{v}</Tag> : '—' },
                    {
                      title: 'Accepted By',
                      dataIndex: 'accepted_by_driver_name',
                      render: (v, r) => v || (r.accepted_by_driver_id ? `Driver #${r.accepted_by_driver_id}` : '—'),
                    },
                    { title: 'Added At', dataIndex: 'added_at', render: (v) => v ? formatDateTimeShort(v) : '—' },
                  ]}
                />
              </>
            )}
          </>
        )}
      </Drawer>

      {/* Force Close Session Modal */}
      <Modal
        title="Force Close Session"
        open={forceCloseOpen}
        onCancel={() => { setForceCloseOpen(false); setForceCloseTarget(null); forceCloseForm.resetFields(); }}
        onOk={() => forceCloseForm.submit()}
        confirmLoading={forceCloseMutation.isLoading}
        okButtonProps={{ danger: true }}
        okText="Force Close"
      >
        <p style={{ marginBottom: 16 }}>
          Driver: <strong>{forceCloseTarget?.driver_name || forceCloseTarget?.driver_user_id}</strong>
          {' '}| Loaded: <strong>{forceCloseTarget?.bottles_loaded}</strong>
        </p>
        <Form
          form={forceCloseForm}
          layout="vertical"
          onFinish={(values) => forceCloseMutation.mutate({ sessionId: forceCloseTarget.id, data: values })}
        >
          <Form.Item name="bottles_returned_to_warehouse" label="Bottles returned to warehouse (if known)" initialValue={0}>
            <InputNumber style={{ width: '100%' }} min={0} />
          </Form.Item>
          <Form.Item name="reason" label="Reason (required)" rules={[{ required: true, message: 'Please enter a reason' }]}>
            <Input.TextArea rows={3} placeholder="Why is this session being force-closed?" />
          </Form.Item>
        </Form>
      </Modal>

      {/* Resolve Transfer Dispute Modal */}
      <Modal
        title="Resolve Transfer Dispute"
        open={resolveOpen}
        onCancel={() => { setResolveOpen(false); setResolveTarget(null); resolveForm.resetFields(); }}
        onOk={() => resolveForm.submit()}
        confirmLoading={resolveTransferMutation.isLoading}
        okText="Resolve"
      >
        {resolveTarget && (
          <p style={{ marginBottom: 16 }}>
            Sender declared <strong>{resolveTarget.declared_quantity}</strong>, receiver confirmed <strong>{resolveTarget.confirmed_quantity}</strong>.
          </p>
        )}
        <Form
          form={resolveForm}
          layout="vertical"
          onFinish={(values) => resolveTransferMutation.mutate({ transferId: resolveTarget.id, data: values })}
        >
          <Form.Item name="resolved_quantity" label="Resolved Quantity" rules={[{ required: true }]}>
            <InputNumber style={{ width: '100%' }} min={0} />
          </Form.Item>
          <Form.Item name="resolution_notes" label="Resolution Notes (required)" rules={[{ required: true, message: 'Please explain the resolution' }]}>
            <Input.TextArea rows={3} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default BottleTracking;
