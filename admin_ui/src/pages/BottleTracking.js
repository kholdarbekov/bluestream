import React, { useMemo, useRef, useState } from 'react';
import { DEFAULT_PAGE_SIZE } from '../utils/constants';
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
import { useMutation, useQuery, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import adminService from '../services/adminService';
import { formatDate, formatDateTimeShort } from '../utils/dateUtils';
import { formatMoney } from '../utils/formatMoney';

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

// Default (English) labels — also used as the t() defaultValue fallback.
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

// --- Dashboard Tab ---
const DashboardStats = ({ stats, loading }) => {
  const { t } = useTranslation('bottle_tracking');
  return (
    <Row gutter={[16, 16]}>
      <Col xs={12} sm={6}>
        <Card>
          <Statistic
            title={t('total_bottles_out', { defaultValue: 'Total Bottles Out' })}
            value={stats?.total_bottles_out ?? 0}
            prefix={<ExclamationCircleOutlined />}
          />
        </Card>
      </Col>
      <Col xs={12} sm={6}>
        <Card>
          <Statistic
            title={t('customers_with_balance', { defaultValue: 'Customers with Balance' })}
            value={stats?.customers_with_balance ?? 0}
          />
        </Card>
      </Col>
      <Col xs={12} sm={6}>
        <Card>
          <Statistic
            title={t('active_fines', { defaultValue: 'Active Fines' })}
            value={stats?.active_fines ?? 0}
            prefix={<WarningOutlined />}
            valueStyle={stats?.active_fines > 0 ? { color: '#cf1322' } : undefined}
          />
        </Card>
      </Col>
      <Col xs={12} sm={6}>
        <Card>
          <Statistic
            title={t('total_fine_amount', { defaultValue: 'Total Fine Amount' })}
            value={formatMoney(stats?.total_fine_amount ?? 0)}
            prefix={<DollarOutlined />}
          />
        </Card>
      </Col>
    </Row>
  );
};

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
  const { t } = useTranslation('bottle_tracking');
  const [searchTerm, setSearchTerm] = useState('');
  const debounceRef = useRef();

  const selectedUserId = Form.useWatch(userFieldName, form);

  const { data: usersData, isFetching: usersFetching } = useQuery({
    queryKey: ['bottle-customer-search', searchTerm],
    queryFn: () => adminService.getUsers({ search: searchTerm, per_page: DEFAULT_PAGE_SIZE }),
    enabled: searchTerm.length >= 2,
    placeholderData: keepPreviousData,
  });

  const { data: selectedUserData } = useQuery({
    queryKey: ['bottle-customer-details', selectedUserId],
    queryFn: () => adminService.getUserDetails(selectedUserId),
    enabled: Boolean(selectedUserId),
  });

  const { data: addressesData, isFetching: addressesFetching } = useQuery({
    queryKey: ['bottle-customer-addresses', selectedUserId],
    queryFn: () => adminService.getUserAddresses(selectedUserId),
    enabled: Boolean(selectedUserId),
  });

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
      <Form.Item name={userFieldName} label={t('customer', { defaultValue: 'Customer' })} rules={[{ required: true, message: t('select_customer_required', { defaultValue: 'Select a customer' }) }]}>
        <Select
          showSearch
          placeholder={t('search_customer_placeholder', { defaultValue: 'Search by phone, name, or company' })}
          filterOption={false}
          onSearch={handleSearch}
          loading={usersFetching}
          options={userOptions}
          onChange={() => form.setFieldValue(addressFieldName, undefined)}
          notFoundContent={searchTerm.length < 2 ? t('type_at_least_2_chars', { defaultValue: 'Type at least 2 characters' }) : (usersFetching ? t('searching', { defaultValue: 'Searching…' }) : t('no_matches', { defaultValue: 'No matches' }))}
        />
      </Form.Item>
      <Form.Item name={addressFieldName} label={t('address', { defaultValue: 'Address' })} rules={[{ required: true, message: t('select_address_required', { defaultValue: 'Select an address' }) }]}>
        <Select
          placeholder={selectedUserId ? t('select_address_required', { defaultValue: 'Select an address' }) : t('select_customer_first', { defaultValue: 'Select customer first' })}
          disabled={!selectedUserId}
          loading={addressesFetching}
          options={addressOptions}
          notFoundContent={!selectedUserId ? t('select_customer_first', { defaultValue: 'Select customer first' }) : t('no_addresses', { defaultValue: 'No addresses' })}
        />
      </Form.Item>
    </>
  );
};

// --- Main Component ---
const BottleTracking = () => {
  const { t } = useTranslation('bottle_tracking');
  // eslint-disable-next-line security/detect-object-injection
  const eventTypeLabel = (val) => t(`event_${val}`, { defaultValue: EVENT_TYPE_LABELS[val] || val });

  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState('balances');

  // Balances state
  const [balanceSearch, setBalanceSearch] = useState('');
  const [balanceMinBalance, setBalanceMinBalance] = useState();
  const [balancePagination, setBalancePagination] = useState({ page: 1, per_page: DEFAULT_PAGE_SIZE });

  // Ledger state
  const [ledgerEventType, setLedgerEventType] = useState();
  const [ledgerPagination, setLedgerPagination] = useState({ page: 1, per_page: DEFAULT_PAGE_SIZE });

  // Fines state
  const [fineStatus, setFineStatus] = useState();
  const [finePagination, setFinePagination] = useState({ page: 1, per_page: DEFAULT_PAGE_SIZE });

  // Sessions state
  const [sessionPagination, setSessionPagination] = useState({ page: 1, per_page: DEFAULT_PAGE_SIZE });
  const [sessionStatusFilter, setSessionStatusFilter] = useState();
  const [sessionOnlyDiscrepancies, setSessionOnlyDiscrepancies] = useState(false);
  const [sessionDetailOpen, setSessionDetailOpen] = useState(false);
  const [sessionDetailTarget, setSessionDetailTarget] = useState(null);
  const [forceCloseOpen, setForceCloseOpen] = useState(false);
  const [forceCloseTarget, setForceCloseTarget] = useState(null);
  const [forceCloseForm] = Form.useForm();

  // Transfers state
  const [transferPagination, setTransferPagination] = useState({ page: 1, per_page: DEFAULT_PAGE_SIZE });
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
  const { data: dashboardData, isLoading: dashboardLoading } = useQuery({
    queryKey: ['bottle-dashboard'],
    queryFn: () => adminService.getBottleDashboard(),
    staleTime: 30_000,
  });

  const balanceFilters = useMemo(
    () => ({
      page: balancePagination.page,
      per_page: balancePagination.per_page,
      search: balanceSearch || undefined,
      min_balance: balanceMinBalance || undefined,
    }),
    [balancePagination, balanceSearch, balanceMinBalance]
  );

  const { data: balancesData, isLoading: balancesLoading } = useQuery({
    queryKey: ['bottle-balances', balanceFilters],
    queryFn: () => adminService.getBottleBalances(balanceFilters),
    placeholderData: keepPreviousData,
  });

  const ledgerFilters = useMemo(
    () => ({
      page: ledgerPagination.page,
      per_page: ledgerPagination.per_page,
      event_type: ledgerEventType || undefined,
    }),
    [ledgerPagination, ledgerEventType]
  );

  const { data: ledgerData, isLoading: ledgerLoading } = useQuery({
    queryKey: ['bottle-ledger', ledgerFilters],
    queryFn: () => adminService.getBottleLedger(ledgerFilters),
    placeholderData: keepPreviousData,
    enabled: activeTab === 'ledger',
  });

  const fineFilters = useMemo(
    () => ({
      page: finePagination.page,
      per_page: finePagination.per_page,
      status: fineStatus || undefined,
    }),
    [finePagination, fineStatus]
  );

  const { data: finesData, isLoading: finesLoading } = useQuery({
    queryKey: ['bottle-fines', fineFilters],
    queryFn: () => adminService.getBottleFines(fineFilters),
    placeholderData: keepPreviousData,
    enabled: activeTab === 'fines',
  });

  const sessionFilters = useMemo(
    () => ({
      page: sessionPagination.page,
      per_page: sessionPagination.per_page,
      status: sessionStatusFilter || undefined,
      only_discrepancies: sessionOnlyDiscrepancies || undefined,
    }),
    [sessionPagination, sessionStatusFilter, sessionOnlyDiscrepancies]
  );

  const { data: sessionsData, isLoading: sessionsLoading } = useQuery({
    queryKey: ['bottle-sessions', sessionFilters],
    queryFn: () => adminService.getBottleSessions(sessionFilters),
    placeholderData: keepPreviousData,
    enabled: activeTab === 'sessions',
  });

  const { data: sessionDetailData, isLoading: sessionDetailLoading } = useQuery({
    queryKey: ['bottle-session-detail', sessionDetailTarget],
    queryFn: () => adminService.getBottleSession(sessionDetailTarget),
    enabled: Boolean(sessionDetailTarget),
  });

  const transferFilters = useMemo(
    () => ({
      page: transferPagination.page,
      per_page: transferPagination.per_page,
      status: transferStatusFilter || undefined,
    }),
    [transferPagination, transferStatusFilter]
  );

  const { data: transfersData, isLoading: transfersLoading } = useQuery({
    queryKey: ['bottle-transfers', transferFilters],
    queryFn: () => adminService.getBottleTransfers(transferFilters),
    placeholderData: keepPreviousData,
    enabled: activeTab === 'transfers',
  });

  // Ledger drawer query
  const { data: addressLedgerData, isLoading: addressLedgerLoading } = useQuery({
    queryKey: ['bottle-address-ledger', ledgerDrawerTarget?.user_id, ledgerDrawerTarget?.address_id],

    queryFn: () => adminService.getBottleLedgerForAddress(
      ledgerDrawerTarget.user_id,
      ledgerDrawerTarget.address_id,
      { per_page: 50 }
    ),

    enabled: Boolean(ledgerDrawerTarget),
  });

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
    queryClient.invalidateQueries({
      queryKey: ['bottle-dashboard'],
    });
    queryClient.invalidateQueries({
      queryKey: ['bottle-balances'],
    });
    queryClient.invalidateQueries({
      queryKey: ['bottle-ledger'],
    });
    queryClient.invalidateQueries({
      queryKey: ['bottle-fines'],
    });
    queryClient.invalidateQueries({
      queryKey: ['bottle-sessions'],
    });
    queryClient.invalidateQueries({
      queryKey: ['bottle-transfers'],
    });
  };

  // --- Mutations ---
  const adjustmentMutation = useMutation({
    mutationFn: (data) => adminService.createBottleAdjustment(data),

    onSuccess: () => {
      message.success(t('balance_adjusted', { defaultValue: 'Balance adjusted' }));
      setAdjustmentOpen(false);
      adjustmentForm.resetFields();
      refreshAll();
    },

    onError: (err) => message.error(err?.response?.data?.error || t('adjust_failed', { defaultValue: 'Failed to adjust balance' })),
  });

  const initialBalanceMutation = useMutation({
    mutationFn: (data) => adminService.setBottleInitialBalance(data),

    onSuccess: () => {
      message.success(t('initial_balance_set', { defaultValue: 'Initial balance set' }));
      setInitialBalanceOpen(false);
      initialBalanceForm.resetFields();
      refreshAll();
    },

    onError: (err) => message.error(err?.response?.data?.error || t('initial_balance_failed', { defaultValue: 'Failed to set initial balance' })),
  });

  const fineCreateMutation = useMutation({
    mutationFn: (data) => adminService.createBottleFine(data),

    onSuccess: () => {
      message.success(t('fine_created', { defaultValue: 'Fine created' }));
      setFineCreateOpen(false);
      fineForm.resetFields();
      refreshAll();
    },

    onError: (err) => message.error(err?.response?.data?.error || t('fine_create_failed', { defaultValue: 'Failed to create fine' })),
  });

  const fineUpdateMutation = useMutation({
    mutationFn: ({ fineId, data }) => adminService.updateBottleFine(fineId, data),

    onSuccess: () => {
      message.success(t('fine_updated_msg', { defaultValue: 'Fine updated' }));
      refreshAll();
    },

    onError: (err) => message.error(err?.response?.data?.error || t('fine_update_failed', { defaultValue: 'Failed to update fine' })),
  });

  const reconcileMutation = useMutation({
    mutationFn: ({ userId, addressId }) => adminService.reconcileBottleBalance(userId, addressId),

    onSuccess: (res) => {
      const diff = res?.data?.difference;
      if (diff && diff !== 0) {
        message.warning(t('reconciled_corrected', { diff, defaultValue: 'Reconciled — balance corrected by {{diff}}' }));
      } else {
        message.success(t('balance_consistent', { defaultValue: 'Balance is consistent' }));
      }
      refreshAll();
    },

    onError: (err) => message.error(err?.response?.data?.error || t('reconcile_failed', { defaultValue: 'Reconciliation failed' })),
  });

  const forceCloseMutation = useMutation({
    mutationFn: ({ sessionId, data }) => adminService.forceCloseBottleSession(sessionId, data),

    onSuccess: () => {
      message.success(t('session_force_closed', { defaultValue: 'Session force-closed' }));
      setForceCloseOpen(false);
      setForceCloseTarget(null);
      forceCloseForm.resetFields();
      queryClient.invalidateQueries({
        queryKey: ['bottle-sessions'],
      });
    },

    onError: (err) => message.error(err?.response?.data?.error || t('force_close_failed', { defaultValue: 'Failed to force-close session' })),
  });

  const resolveTransferMutation = useMutation({
    mutationFn: ({ transferId, data }) => adminService.resolveBottleTransferDispute(transferId, data),

    onSuccess: () => {
      message.success(t('transfer_resolved', { defaultValue: 'Transfer dispute resolved' }));
      setResolveOpen(false);
      setResolveTarget(null);
      resolveForm.resetFields();
      queryClient.invalidateQueries({
        queryKey: ['bottle-transfers'],
      });
      queryClient.invalidateQueries({
        queryKey: ['bottle-sessions'],
      });
    },

    onError: (err) => message.error(err?.response?.data?.error || t('resolve_failed', { defaultValue: 'Failed to resolve dispute' })),
  });

  // --- Balance columns ---
  const balanceColumns = [
    {
      title: t('customer', { defaultValue: 'Customer' }),
      key: 'customer',
      render: (_, record) => (
        <Space direction="vertical" size={0}>
          <Text strong>{record.customer_name || record.user_name || `User #${record.user_id}`}</Text>
          <Text type="secondary">{record.customer_phone || record.user_phone || ''}</Text>
        </Space>
      ),
    },
    {
      title: t('address', { defaultValue: 'Address' }),
      key: 'address',
      render: (_, record) => record.address_title || record.address_label || `Address #${record.address_id}`,
    },
    {
      title: t('balance', { defaultValue: 'Balance' }),
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
      title: t('last_delivery', { defaultValue: 'Last Delivery' }),
      dataIndex: 'last_delivery_at',
      key: 'last_delivery_at',
      render: (val) => (val ? formatDateTimeShort(val) : '—'),
    },
    {
      title: t('last_return', { defaultValue: 'Last Return' }),
      dataIndex: 'last_return_at',
      key: 'last_return_at',
      render: (val) => (val ? formatDateTimeShort(val) : '—'),
    },
    {
      title: t('actions', { defaultValue: 'Actions' }),
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
            {t('ledger_button', { defaultValue: 'Ledger' })}
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
            {t('adjust_button', { defaultValue: 'Adjust' })}
          </Button>
          <Button
            size="small"
            icon={<SyncOutlined />}
            loading={reconcileMutation.isPending}
            onClick={() => reconcileMutation.mutate({ userId: record.user_id, addressId: record.address_id })}
          >
            {t('reconcile_button', { defaultValue: 'Reconcile' })}
          </Button>
        </Space>
      ),
    },
  ];

  // --- Ledger columns ---
  const ledgerColumns = [
    {
      title: t('date', { defaultValue: 'Date' }),
      dataIndex: 'occurred_at',
      key: 'occurred_at',
      render: (val) => (val ? formatDateTimeShort(val) : '—'),
      width: 160,
    },
    {
      title: t('customer', { defaultValue: 'Customer' }),
      key: 'customer',
      render: (_, record) => record.customer_name || record.user_name || `User #${record.user_id}`,
    },
    {
      title: t('address', { defaultValue: 'Address' }),
      key: 'address',
      render: (_, record) => record.address_title || `Address #${record.address_id}`,
    },
    {
      title: t('event', { defaultValue: 'Event' }),
      dataIndex: 'event_type',
      key: 'event_type',
      render: (val) => (
        // eslint-disable-next-line security/detect-object-injection
        <Tag color={EVENT_TYPE_COLORS[val] || 'default'}>
          {eventTypeLabel(val)}
        </Tag>
      ),
    },
    {
      title: t('quantity', { defaultValue: 'Quantity' }),
      dataIndex: 'quantity',
      key: 'quantity',
      render: (val) => {
        const num = Number(val) || 0;
        const color = num > 0 ? '#cf1322' : num < 0 ? '#389e0d' : undefined;
        return <Text strong style={{ color }}>{num > 0 ? `+${num}` : num}</Text>;
      },
    },
    {
      title: t('balance_after', { defaultValue: 'Balance After' }),
      dataIndex: 'balance_after',
      key: 'balance_after',
      render: (val) => Number(val) || 0,
    },
    {
      title: t('actor', { defaultValue: 'Actor' }),
      key: 'actor',
      render: (_, record) => record.actor_name || record.actor_user_name || '—',
    },
    {
      title: t('notes', { defaultValue: 'Notes' }),
      dataIndex: 'notes',
      key: 'notes',
      ellipsis: true,
    },
  ];

  // --- Fine columns ---
  const fineColumns = [
    {
      title: t('customer', { defaultValue: 'Customer' }),
      key: 'customer',
      render: (_, record) => record.customer_name || record.user_name || `User #${record.user_id}`,
    },
    {
      title: t('quantity', { defaultValue: 'Quantity' }),
      dataIndex: 'quantity',
      key: 'quantity',
    },
    {
      title: t('fine_amount', { defaultValue: 'Fine Amount' }),
      dataIndex: 'fine_amount',
      key: 'fine_amount',
      render: (val) => formatMoney(val),
    },
    {
      title: t('status', { defaultValue: 'Status' }),
      dataIndex: 'status',
      key: 'status',
      render: (val) => (
        // eslint-disable-next-line security/detect-object-injection
        <Tag color={FINE_STATUS_COLORS[val] || 'default'}>
          {(val || '').toUpperCase()}
        </Tag>
      ),
    },
    {
      title: t('issued_by', { defaultValue: 'Issued By' }),
      key: 'issued_by',
      render: (_, record) => record.issued_by_name || '—',
    },
    {
      title: t('issued_at', { defaultValue: 'Issued At' }),
      dataIndex: 'issued_at',
      key: 'issued_at',
      render: (val) => (val ? formatDate(val) : '—'),
    },
    {
      title: t('notes', { defaultValue: 'Notes' }),
      dataIndex: 'notes',
      key: 'notes',
      ellipsis: true,
    },
    {
      title: t('actions', { defaultValue: 'Actions' }),
      key: 'actions',
      render: (_, record) => {
        if (record.status === 'paid' || record.status === 'waived') return null;
        return (
          <Space>
            <Button
              size="small"
              type="primary"
              onClick={() => fineUpdateMutation.mutate({ fineId: record.id, data: { action: 'mark_paid' } })}
              loading={fineUpdateMutation.isPending}
            >
              {t('mark_paid', { defaultValue: 'Mark Paid' })}
            </Button>
            <Button
              size="small"
              danger
              onClick={() => fineUpdateMutation.mutate({ fineId: record.id, data: { action: 'waive', notes: 'Waived by admin' } })}
              loading={fineUpdateMutation.isPending}
            >
              {t('waive', { defaultValue: 'Waive' })}
            </Button>
          </Space>
        );
      },
    },
  ];

  // --- Address ledger columns (drawer) ---
  const addressLedgerColumns = [
    {
      title: t('date', { defaultValue: 'Date' }),
      dataIndex: 'occurred_at',
      key: 'occurred_at',
      render: (val) => (val ? formatDateTimeShort(val) : '—'),
    },
    {
      title: t('event', { defaultValue: 'Event' }),
      dataIndex: 'event_type',
      key: 'event_type',
      render: (val) => (
        // eslint-disable-next-line security/detect-object-injection
        <Tag color={EVENT_TYPE_COLORS[val] || 'default'}>
          {eventTypeLabel(val)}
        </Tag>
      ),
    },
    {
      title: t('qty', { defaultValue: 'Qty' }),
      dataIndex: 'quantity',
      key: 'quantity',
      render: (val) => {
        const num = Number(val) || 0;
        const color = num > 0 ? '#cf1322' : num < 0 ? '#389e0d' : undefined;
        return <Text strong style={{ color }}>{num > 0 ? `+${num}` : num}</Text>;
      },
    },
    {
      title: t('balance_after', { defaultValue: 'Balance After' }),
      dataIndex: 'balance_after',
      key: 'balance_after',
    },
    { title: t('notes', { defaultValue: 'Notes' }), dataIndex: 'notes', key: 'notes', ellipsis: true },
  ];

  const addressLedgerEntries = addressLedgerData?.data?.items || addressLedgerData?.data || [];

  // --- Tab items ---
  const tabItems = [
    {
      key: 'balances',
      label: t('tab_balances', { defaultValue: 'Balances' }),
      children: (
        <>
          <Space style={{ marginBottom: 16 }} wrap>
            <Input.Search
              placeholder={t('search_customer_balance_placeholder', { defaultValue: 'Search customer...' })}
              allowClear
              style={{ width: 250 }}
              onSearch={(val) => {
                setBalanceSearch(val);
                setBalancePagination((p) => ({ ...p, page: 1 }));
              }}
            />
            <InputNumber
              placeholder={t('min_balance_placeholder', { defaultValue: 'Min balance' })}
              min={1}
              style={{ width: 130 }}
              onChange={(val) => {
                setBalanceMinBalance(val);
                setBalancePagination((p) => ({ ...p, page: 1 }));
              }}
            />
            <Button icon={<PlusOutlined />} onClick={() => setInitialBalanceOpen(true)}>
              {t('set_initial_balance', { defaultValue: 'Set Initial Balance' })}
            </Button>
            <Button icon={<EditOutlined />} onClick={() => setAdjustmentOpen(true)}>
              {t('adjust_balance', { defaultValue: 'Adjust Balance' })}
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
      label: t('tab_ledger', { defaultValue: 'Ledger' }),
      children: (
        <>
          <Space style={{ marginBottom: 16 }} wrap>
            <Select
              placeholder={t('event_type_placeholder', { defaultValue: 'Event type' })}
              allowClear
              style={{ width: 200 }}
              onChange={(val) => {
                setLedgerEventType(val);
                setLedgerPagination((p) => ({ ...p, page: 1 }));
              }}
              options={Object.keys(EVENT_TYPE_LABELS).map((value) => ({ value, label: eventTypeLabel(value) }))}
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
      label: t('tab_fines', { defaultValue: 'Fines' }),
      children: (
        <>
          <Space style={{ marginBottom: 16 }} wrap>
            <Select
              placeholder={t('status_placeholder', { defaultValue: 'Status' })}
              allowClear
              style={{ width: 150 }}
              onChange={(val) => {
                setFineStatus(val);
                setFinePagination((p) => ({ ...p, page: 1 }));
              }}
              options={[
                { value: 'pending', label: t('fine_status_pending', { defaultValue: 'Pending' }) },
                { value: 'invoiced', label: t('fine_status_invoiced', { defaultValue: 'Invoiced' }) },
                { value: 'paid', label: t('fine_status_paid', { defaultValue: 'Paid' }) },
                { value: 'waived', label: t('fine_status_waived', { defaultValue: 'Waived' }) },
              ]}
            />
            <Button icon={<PlusOutlined />} onClick={() => setFineCreateOpen(true)}>
              {t('create_fine', { defaultValue: 'Create Fine' })}
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
      label: t('tab_sessions', { defaultValue: 'Driver Sessions' }),
      children: (
        <>
          <Space wrap style={{ marginBottom: 12 }}>
            <Select
              allowClear
              placeholder={t('status_placeholder', { defaultValue: 'Status' })}
              style={{ width: 140 }}
              value={sessionStatusFilter}
              onChange={(val) => {
                setSessionStatusFilter(val);
                setSessionPagination((p) => ({ ...p, page: 1 }));
              }}
              options={[
                { value: 'open', label: t('session_status_open', { defaultValue: 'Open' }) },
                { value: 'closed', label: t('session_status_closed', { defaultValue: 'Closed' }) },
                { value: 'force_closed', label: t('session_status_force_closed', { defaultValue: 'Force Closed' }) },
                { value: 'cancelled', label: t('session_status_cancelled', { defaultValue: 'Cancelled' }) },
              ]}
            />
            <Button
              type={sessionOnlyDiscrepancies ? 'primary' : 'default'}
              icon={<WarningOutlined />}
              onClick={() => {
                setSessionOnlyDiscrepancies((v) => !v);
                setSessionPagination((p) => ({ ...p, page: 1 }));
              }}
            >
              {t('discrepancies_only', { defaultValue: 'Discrepancies only' })}
            </Button>
          </Space>
          <Table
            columns={[
              { title: t('driver', { defaultValue: 'Driver' }), dataIndex: 'driver_name', key: 'driver_name', render: (v, r) => v || r.driver_user_id },
              { title: t('started', { defaultValue: 'Started' }), dataIndex: 'started_at', key: 'started_at', render: (v) => v ? formatDateTimeShort(v) : '—' },
              { title: t('closed', { defaultValue: 'Closed' }), dataIndex: 'closed_at', key: 'closed_at', render: (v) => v ? formatDateTimeShort(v) : '—' },
              { title: t('loaded', { defaultValue: 'Loaded' }), dataIndex: 'bottles_loaded', key: 'bottles_loaded', align: 'right' },
              { title: t('delivered', { defaultValue: 'Delivered' }), dataIndex: 'bottles_delivered', key: 'bottles_delivered', align: 'right' },
              { title: t('collected', { defaultValue: 'Collected' }), dataIndex: 'bottles_collected_from_customers', key: 'bottles_collected_from_customers', align: 'right' },
              { title: t('on_truck', { defaultValue: 'On Truck' }), dataIndex: 'current_inventory', key: 'current_inventory', align: 'right' },
              { title: t('returned', { defaultValue: 'Returned' }), dataIndex: 'bottles_returned_to_warehouse', key: 'bottles_returned_to_warehouse', align: 'right', render: (v) => v ?? '—' },
              {
                title: t('discrepancy', { defaultValue: 'Discrepancy' }), dataIndex: 'discrepancy', key: 'discrepancy', align: 'right',
                render: (v) => {
                  if (v == null) return '—';
                  return <Tag color={v === 0 ? 'green' : 'red'}>{v === 0 ? '✓ 0' : v}</Tag>;
                },
              },
              {
                title: t('status', { defaultValue: 'Status' }), dataIndex: 'status', key: 'status',
                render: (v) => {
                  const colors = { open: 'blue', closed: 'green', force_closed: 'red', cancelled: 'default' };
                  // eslint-disable-next-line security/detect-object-injection
                  return <Tag color={colors[v] || 'default'}>{(v || '').toUpperCase()}</Tag>;
                },
              },
              {
                title: t('actions', { defaultValue: 'Actions' }), key: 'actions',
                render: (_, record) => (
                  <Space>
                    <Button size="small" icon={<EyeOutlined />} onClick={() => { setSessionDetailTarget(record.id); setSessionDetailOpen(true); }}>
                      {t('detail', { defaultValue: 'Detail' })}
                    </Button>
                    {record.status === 'open' && (
                      <Button size="small" danger icon={<ExclamationCircleOutlined />} onClick={() => { setForceCloseTarget(record); setForceCloseOpen(true); }}>
                        {t('force_close', { defaultValue: 'Force Close' })}
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
      label: t('tab_transfers', { defaultValue: 'Bottle Transfers' }),
      children: (
        <>
          <Space wrap style={{ marginBottom: 12 }}>
            <Select
              allowClear
              placeholder={t('status_placeholder', { defaultValue: 'Status' })}
              style={{ width: 150 }}
              value={transferStatusFilter}
              onChange={(val) => {
                setTransferStatusFilter(val);
                setTransferPagination((p) => ({ ...p, page: 1 }));
              }}
              options={[
                { value: 'pending', label: t('transfer_status_pending', { defaultValue: 'Pending' }) },
                { value: 'confirmed', label: t('transfer_status_confirmed', { defaultValue: 'Confirmed' }) },
                { value: 'disputed', label: t('transfer_status_disputed', { defaultValue: 'Disputed' }) },
                { value: 'resolved', label: t('transfer_status_resolved', { defaultValue: 'Resolved' }) },
              ]}
            />
          </Space>
          <Table
            columns={[
              { title: t('sender', { defaultValue: 'Sender' }), dataIndex: 'sender_name', key: 'sender_name', render: (v, r) => v || r.sender_driver_id },
              { title: t('receiver', { defaultValue: 'Receiver' }), dataIndex: 'receiver_name', key: 'receiver_name', render: (v, r) => v || r.receiver_driver_id },
              { title: t('declared', { defaultValue: 'Declared' }), dataIndex: 'declared_quantity', key: 'declared_quantity', align: 'right' },
              { title: t('confirmed', { defaultValue: 'Confirmed' }), dataIndex: 'confirmed_quantity', key: 'confirmed_quantity', align: 'right', render: (v) => v ?? '—' },
              { title: t('sent_at', { defaultValue: 'Sent At' }), dataIndex: 'sent_at', key: 'sent_at', render: (v) => v ? formatDateTimeShort(v) : '—' },
              {
                title: t('status', { defaultValue: 'Status' }), dataIndex: 'status', key: 'status',
                render: (v) => {
                  const colors = { pending: 'blue', confirmed: 'green', disputed: 'red', resolved: 'default' };
                  // eslint-disable-next-line security/detect-object-injection
                  return <Tag color={colors[v] || 'default'}>{(v || '').toUpperCase()}</Tag>;
                },
              },
              {
                title: t('actions', { defaultValue: 'Actions' }), key: 'actions',
                render: (_, record) => record.status === 'disputed' ? (
                  <Button size="small" icon={<EditOutlined />} onClick={() => { setResolveTarget(record); setResolveOpen(true); }}>
                    {t('resolve', { defaultValue: 'Resolve' })}
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
          {t('page_title', { defaultValue: 'Bottle Tracking' })}
        </Typography.Title>
        <Button icon={<ReloadOutlined />} onClick={refreshAll}>
          {t('refresh', { defaultValue: 'Refresh' })}
        </Button>
      </div>

      <DashboardStats stats={dashboard} loading={dashboardLoading} />

      <Card style={{ marginTop: 16 }}>
        <Tabs activeKey={activeTab} onChange={setActiveTab} items={tabItems} />
      </Card>

      {/* Adjustment Modal */}
      <Modal
        title={t('adjust_balance_title', { defaultValue: 'Adjust Bottle Balance' })}
        open={adjustmentOpen}
        onCancel={() => { setAdjustmentOpen(false); adjustmentForm.resetFields(); }}
        onOk={() => adjustmentForm.submit()}
        confirmLoading={adjustmentMutation.isPending}
      >
        <Form
          form={adjustmentForm}
          layout="vertical"
          onFinish={(values) => adjustmentMutation.mutate(values)}
        >
          <CustomerAddressFields form={adjustmentForm} />
          <Form.Item
            name="adjustment"
            label={t('adjustment_label', { defaultValue: 'Adjustment (positive = add bottles to customer, negative = remove)' })}
            rules={[{ required: true }]}
          >
            <InputNumber style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="notes" label={t('notes', { defaultValue: 'Notes' })} rules={[{ required: true }]}>
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      {/* Initial Balance Modal */}
      <Modal
        title={t('set_initial_balance_title', { defaultValue: 'Set Initial Bottle Balance' })}
        open={initialBalanceOpen}
        onCancel={() => { setInitialBalanceOpen(false); initialBalanceForm.resetFields(); }}
        onOk={() => initialBalanceForm.submit()}
        confirmLoading={initialBalanceMutation.isPending}
      >
        <Form
          form={initialBalanceForm}
          layout="vertical"
          onFinish={(values) => initialBalanceMutation.mutate(values)}
        >
          <CustomerAddressFields form={initialBalanceForm} />
          <Form.Item name="quantity" label={t('bottle_quantity_label', { defaultValue: 'Bottle Quantity' })} rules={[{ required: true }]}>
            <InputNumber style={{ width: '100%' }} min={0} />
          </Form.Item>
          <Form.Item name="notes" label={t('notes', { defaultValue: 'Notes' })}>
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      {/* Create Fine Modal */}
      <Modal
        title={t('create_fine_title', { defaultValue: 'Create Bottle Fine' })}
        open={fineCreateOpen}
        onCancel={() => { setFineCreateOpen(false); fineForm.resetFields(); }}
        onOk={() => fineForm.submit()}
        confirmLoading={fineCreateMutation.isPending}
      >
        <Form
          form={fineForm}
          layout="vertical"
          onFinish={(values) => fineCreateMutation.mutate(values)}
        >
          <CustomerAddressFields form={fineForm} />
          <Form.Item name="quantity" label={t('bottles_to_fine_label', { defaultValue: 'Bottles to Fine For' })} rules={[{ required: true }]}>
            <InputNumber style={{ width: '100%' }} min={1} />
          </Form.Item>
          <Form.Item name="fine_amount" label={t('fine_amount', { defaultValue: 'Fine Amount' })} rules={[{ required: true }]}>
            <InputNumber style={{ width: '100%' }} min={0} />
          </Form.Item>
          <Form.Item name="notes" label={t('notes', { defaultValue: 'Notes' })}>
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      {/* Address Ledger Drawer */}
      <Drawer
        title={t('address_ledger_title', { defaultValue: 'Address Bottle Ledger' })}
        open={ledgerDrawerOpen}
        onClose={() => { setLedgerDrawerOpen(false); setLedgerDrawerTarget(null); }}
        width={700}
      >
        {ledgerDrawerTarget && (
          <Descriptions column={2} size="small" style={{ marginBottom: 16 }}>
            <Descriptions.Item label={t('user_id_label', { defaultValue: 'User ID' })}>{ledgerDrawerTarget.user_id}</Descriptions.Item>
            <Descriptions.Item label={t('address_id_label', { defaultValue: 'Address ID' })}>{ledgerDrawerTarget.address_id}</Descriptions.Item>
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
        title={t('session_detail_title', { defaultValue: 'Session Detail' })}
        open={sessionDetailOpen}
        onClose={() => { setSessionDetailOpen(false); setSessionDetailTarget(null); }}
        width={600}
      >
        {sessionDetail && (
          <>
            <Descriptions column={2} size="small" bordered style={{ marginBottom: 16 }}>
              <Descriptions.Item label={t('driver', { defaultValue: 'Driver' })}>{sessionDetail.driver_name || sessionDetail.driver_user_id}</Descriptions.Item>
              <Descriptions.Item label={t('status', { defaultValue: 'Status' })}>
                <Tag color={{ open: 'blue', closed: 'green', force_closed: 'red', cancelled: 'default' }[sessionDetail.status] || 'default'}>
                  {(sessionDetail.status || '').toUpperCase()}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t('started', { defaultValue: 'Started' })}>{sessionDetail.started_at ? formatDateTimeShort(sessionDetail.started_at) : '—'}</Descriptions.Item>
              <Descriptions.Item label={t('closed', { defaultValue: 'Closed' })}>{sessionDetail.closed_at ? formatDateTimeShort(sessionDetail.closed_at) : '—'}</Descriptions.Item>
              <Descriptions.Item label={t('loaded', { defaultValue: 'Loaded' })}>{sessionDetail.bottles_loaded}</Descriptions.Item>
              <Descriptions.Item label={t('delivered', { defaultValue: 'Delivered' })}>{sessionDetail.bottles_delivered}</Descriptions.Item>
              <Descriptions.Item label={t('collected', { defaultValue: 'Collected' })}>{sessionDetail.bottles_collected_from_customers}</Descriptions.Item>
              <Descriptions.Item label={t('transferred_out', { defaultValue: 'Transferred Out' })}>{sessionDetail.bottles_transferred_out}</Descriptions.Item>
              <Descriptions.Item label={t('transferred_in', { defaultValue: 'Transferred In' })}>{sessionDetail.bottles_transferred_in}</Descriptions.Item>
              <Descriptions.Item label={t('returned_to_wh', { defaultValue: 'Returned to WH' })}>{sessionDetail.bottles_returned_to_warehouse ?? '—'}</Descriptions.Item>
              <Descriptions.Item label={t('discrepancy', { defaultValue: 'Discrepancy' })}>
                {sessionDetail.discrepancy != null
                  ? <Tag color={sessionDetail.discrepancy === 0 ? 'green' : 'red'}>{sessionDetail.discrepancy}</Tag>
                  : '—'}
              </Descriptions.Item>
              {sessionDetail.force_close_reason && (
                <Descriptions.Item label={t('force_close_reason_label', { defaultValue: 'Force Close Reason' })} span={2}>{sessionDetail.force_close_reason}</Descriptions.Item>
              )}
            </Descriptions>
            {sessionDetail.members?.length > 0 && (
              <>
                <Text strong style={{ display: 'block', marginBottom: 8 }}>
                  {t('co_drivers_heading', { count: sessionDetail.members.length, defaultValue: 'Co-Drivers ({{count}})' })}
                </Text>
                <Table
                  style={{ marginBottom: 16 }}
                  size="small"
                  pagination={false}
                  dataSource={sessionDetail.members}
                  rowKey={(r) => r.membership_id || r.member_driver_id}
                  columns={[
                    { title: t('driver', { defaultValue: 'Driver' }), dataIndex: 'member_name', render: (v, r) => v || `Driver #${r.member_driver_id}` },
                    {
                      title: t('status', { defaultValue: 'Status' }),
                      dataIndex: 'status',
                      render: (v) => (
                        // eslint-disable-next-line security/detect-object-injection
                        <Tag color={{ active: 'blue', left: 'orange', revoked: 'red' }[v] || 'default'}>
                          {(v || '').toUpperCase()}
                        </Tag>
                      ),
                    },
                    { title: t('joined', { defaultValue: 'Joined' }), dataIndex: 'joined_at', render: (v) => v ? formatDateTimeShort(v) : '—' },
                    { title: t('left', { defaultValue: 'Left' }), dataIndex: 'left_at', render: (v) => v ? formatDateTimeShort(v) : '—' },
                  ]}
                />
              </>
            )}
            {sessionDetail.orders?.length > 0 && (
              <>
                <Text strong>{t('bound_orders_heading', { count: sessionDetail.orders.length, defaultValue: 'Bound Orders ({{count}})' })}</Text>
                <Table
                  style={{ marginTop: 8 }}
                  size="small"
                  pagination={false}
                  dataSource={sessionDetail.orders}
                  rowKey="order_id"
                  columns={[
                    { title: t('order_number', { defaultValue: 'Order #' }), dataIndex: 'order_number', render: (v) => v ?? '—' },
                    { title: t('customer', { defaultValue: 'Customer' }), dataIndex: 'customer_name', render: (v) => v ?? '—' },
                    {
                      title: t('items', { defaultValue: 'Items' }),
                      dataIndex: 'items',
                      render: (items) =>
                        items?.length
                          ? items.map((i) => `${i.product_name} ×${i.quantity}`).join(', ')
                          : '—',
                    },
                    { title: t('total', { defaultValue: 'Total' }), dataIndex: 'total_amount', render: (v) => formatMoney(v) },
                    { title: t('status', { defaultValue: 'Status' }), dataIndex: 'status', render: (v) => v ? <Tag>{v}</Tag> : '—' },
                    {
                      title: t('accepted_by', { defaultValue: 'Accepted By' }),
                      dataIndex: 'accepted_by_driver_name',
                      render: (v, r) => v || (r.accepted_by_driver_id ? `Driver #${r.accepted_by_driver_id}` : '—'),
                    },
                    { title: t('added_at', { defaultValue: 'Added At' }), dataIndex: 'added_at', render: (v) => v ? formatDateTimeShort(v) : '—' },
                  ]}
                />
              </>
            )}
          </>
        )}
      </Drawer>

      {/* Force Close Session Modal */}
      <Modal
        title={t('force_close_title', { defaultValue: 'Force Close Session' })}
        open={forceCloseOpen}
        onCancel={() => { setForceCloseOpen(false); setForceCloseTarget(null); forceCloseForm.resetFields(); }}
        onOk={() => forceCloseForm.submit()}
        confirmLoading={forceCloseMutation.isPending}
        okButtonProps={{ danger: true }}
        okText={t('force_close', { defaultValue: 'Force Close' })}
      >
        <p style={{ marginBottom: 16 }}>
          {t('driver_prefix', { defaultValue: 'Driver:' })} <strong>{forceCloseTarget?.driver_name || forceCloseTarget?.driver_user_id}</strong>
          {' '}{t('loaded_prefix', { defaultValue: '| Loaded:' })} <strong>{forceCloseTarget?.bottles_loaded}</strong>
        </p>
        <Form
          form={forceCloseForm}
          layout="vertical"
          onFinish={(values) => forceCloseMutation.mutate({ sessionId: forceCloseTarget.id, data: values })}
        >
          <Form.Item name="bottles_returned_to_warehouse" label={t('bottles_returned_wh_label', { defaultValue: 'Bottles returned to warehouse (if known)' })} initialValue={0}>
            <InputNumber style={{ width: '100%' }} min={0} />
          </Form.Item>
          <Form.Item name="reason" label={t('reason_label', { defaultValue: 'Reason (required)' })} rules={[{ required: true, message: t('reason_required_msg', { defaultValue: 'Please enter a reason' }) }]}>
            <Input.TextArea rows={3} placeholder={t('reason_placeholder', { defaultValue: 'Why is this session being force-closed?' })} />
          </Form.Item>
        </Form>
      </Modal>

      {/* Resolve Transfer Dispute Modal */}
      <Modal
        title={t('resolve_dispute_title', { defaultValue: 'Resolve Transfer Dispute' })}
        open={resolveOpen}
        onCancel={() => { setResolveOpen(false); setResolveTarget(null); resolveForm.resetFields(); }}
        onOk={() => resolveForm.submit()}
        confirmLoading={resolveTransferMutation.isPending}
        okText={t('resolve', { defaultValue: 'Resolve' })}
      >
        {resolveTarget && (
          <p style={{ marginBottom: 16 }}>
            {t('sender_declared_prefix', { defaultValue: 'Sender declared' })} <strong>{resolveTarget.declared_quantity}</strong>{t('receiver_confirmed_infix', { defaultValue: ', receiver confirmed' })} <strong>{resolveTarget.confirmed_quantity}</strong>.
          </p>
        )}
        <Form
          form={resolveForm}
          layout="vertical"
          onFinish={(values) => resolveTransferMutation.mutate({ transferId: resolveTarget.id, data: values })}
        >
          <Form.Item name="resolved_quantity" label={t('resolved_quantity_label', { defaultValue: 'Resolved Quantity' })} rules={[{ required: true }]}>
            <InputNumber style={{ width: '100%' }} min={0} />
          </Form.Item>
          <Form.Item name="resolution_notes" label={t('resolution_notes_label', { defaultValue: 'Resolution Notes (required)' })} rules={[{ required: true, message: t('resolution_notes_required_msg', { defaultValue: 'Please explain the resolution' }) }]}>
            <Input.TextArea rows={3} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default BottleTracking;
