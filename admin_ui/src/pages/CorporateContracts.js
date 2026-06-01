import React, { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Divider,
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
  Tag,
  message
} from 'antd';
import {
  BankOutlined,
  DollarCircleOutlined,
  PlusOutlined,
  ReloadOutlined
} from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import adminService from '../services/adminService';
import { formatDateTimeShort } from '../utils/dateUtils';
import { fetchAllPages } from '../utils/pagination';
import { BULK_LOAD_PAGE_SIZE, DEFAULT_PAGE_SIZE } from '../utils/constants';

const { Search, TextArea } = Input;
const { Option } = Select;

const statusColors = {
  draft: 'default',
  active: 'green',
  suspended: 'orange',
  terminated: 'red'
};

const isEntityUser = (user) => String(user?.user_type || '').toLowerCase() === 'entity';

const formatUnits = (value) => Number(value || 0).toLocaleString();
const formatAmount = (value) => Number(value || 0).toLocaleString();

const buildContractPayload = (values) => ({
  user_id: values.user_id,
  contract_number: values.contract_number,
  name: values.name,
  status: values.status,
  start_date: values.start_date || undefined,
  end_date: values.end_date || undefined,
  currency: values.currency || 'UZS',
  notes: values.notes || '',
  is_active: values.is_active !== false,
  is_loyalty_points_eligible: values.is_loyalty_points_eligible === true,
  allows_debt: values.allows_debt === true,
  bank_details: {
    account_number: values.account_number || '',
    bank_name: values.bank_name || '',
    mfo: values.mfo || '',
    inn: values.inn || ''
  }
});

const CorporateContracts = () => {
  const { t } = useTranslation(['navigation', 'common']);
  const queryClient = useQueryClient();

  const [searchText, setSearchText] = useState('');
  const [pagination, setPagination] = useState({ page: 1, per_page: DEFAULT_PAGE_SIZE });
  const [selectedContractId, setSelectedContractId] = useState(null);
  const [isContractModalOpen, setIsContractModalOpen] = useState(false);
  const [isTopupModalOpen, setIsTopupModalOpen] = useState(false);
  const [isPricesDrawerOpen, setIsPricesDrawerOpen] = useState(false);
  const [editingContract, setEditingContract] = useState(null);
  const [contractOverlapPreview, setContractOverlapPreview] = useState(null);
  const [pricesOverlapPreview, setPricesOverlapPreview] = useState(null);

  const [contractForm] = Form.useForm();
  const [topupForm] = Form.useForm();
  const [pricesForm] = Form.useForm();

  const contractsQuery = useQuery({
    queryKey: ['corporate-contracts', pagination],

    queryFn: () => adminService.getCorporateContracts({
      page: pagination.page,
      per_page: pagination.per_page
    }),

    placeholderData: keepPreviousData,
  });

  const usersQuery = useQuery({
    queryKey: ['corporate-contract-users'],
    queryFn: () => fetchAllPages(
      (page) => adminService.getUsers({ page, per_page: BULK_LOAD_PAGE_SIZE }),
      (resp) => resp?.data?.items || [],
      BULK_LOAD_PAGE_SIZE,
    ),
    enabled: isContractModalOpen,
  });

  const productsQuery = useQuery({
    queryKey: ['corporate-contract-products'],
    queryFn: () => fetchAllPages(
      (page) => adminService.getProducts({ page, per_page: BULK_LOAD_PAGE_SIZE, is_active: true }),
      (resp) => resp?.data?.items || [],
      BULK_LOAD_PAGE_SIZE,
    ),
    enabled: isPricesDrawerOpen,
  });

  const contractDetailQuery = useQuery({
    queryKey: ['corporate-contract-detail', selectedContractId],
    queryFn: () => adminService.getCorporateContract(selectedContractId),
    enabled: Boolean(selectedContractId),
  });

const contractBalanceQuery = useQuery({
  queryKey: ['corporate-contract-balance', selectedContractId],
  queryFn: () => adminService.getCorporateContractBalance(selectedContractId),
  enabled: Boolean(selectedContractId),
});

  const contractLedgerQuery = useQuery({
    queryKey: ['corporate-contract-ledger', selectedContractId],
    queryFn: () => adminService.getCorporateContractLedger(selectedContractId, { per_page: 50 }),
    enabled: Boolean(selectedContractId),
  });

  const contracts = contractsQuery.data?.data?.items || [];
  const selectedContract = contractDetailQuery.data?.data?.contract || null;
  const selectedBalance = contractBalanceQuery.data?.data?.balance || null;
  const balanceProducts = selectedBalance?.products || [];
  const balanceSummary = selectedBalance?.summary || {};
  const ledgerItems = contractLedgerQuery.data?.data?.items || [];
  const availableUsers = usersQuery.data || [];
  const availableProducts = productsQuery.data || [];
  const corporateUsers = useMemo(
    () => availableUsers.filter((user) => isEntityUser(user)),
    [availableUsers]
  );

  const visibleContracts = useMemo(() => {
    const normalizedSearch = searchText.trim().toLowerCase();
    if (!normalizedSearch) {
      return contracts;
    }

    return contracts.filter((contract) => {
      const customerName = `${contract.user?.first_name || ''} ${contract.user?.last_name || ''}`.trim();
      return [
        contract.contract_number,
        contract.name,
        customerName,
        contract.user?.phone,
        contract.user?.email
      ].some((value) => String(value || '').toLowerCase().includes(normalizedSearch));
    });
  }, [contracts, searchText]);

  useEffect(() => {
    if (!selectedContractId && contracts.length > 0) {
      setSelectedContractId(contracts[0].id);
    }
  }, [contracts, selectedContractId]);

  const invalidateCorporateQueries = () => {
    queryClient.invalidateQueries({
      queryKey: ['corporate-contracts'],
    });
    queryClient.invalidateQueries({
      queryKey: ['corporate-contract-detail'],
    });
    queryClient.invalidateQueries({
      queryKey: ['corporate-contract-balance'],
    });
    queryClient.invalidateQueries({
      queryKey: ['corporate-contract-ledger'],
    });
  };

  const createContractMutation = useMutation({
    mutationFn: (payload) => adminService.createCorporateContract(payload),

    onSuccess: (response) => {
      const createdContract = response?.data?.contract;
      message.success(t('ui.corporate.contract_created', 'Corporate contract created'));
      setIsContractModalOpen(false);
      contractForm.resetFields();
      invalidateCorporateQueries();
      if (createdContract?.id) {
        setSelectedContractId(createdContract.id);
      }
    },

    onError: (error) => {
      message.error(error.response?.data?.message || t('ui.corporate.contract_create_failed', 'Failed to create contract'));
    },
  });

  const updateContractMutation = useMutation({
    mutationFn: ({ contractId, payload }) => adminService.updateCorporateContract(contractId, payload),

    onSuccess: () => {
      message.success(t('ui.corporate.contract_updated', 'Corporate contract updated'));
      setIsContractModalOpen(false);
      setEditingContract(null);
      contractForm.resetFields();
      invalidateCorporateQueries();
    },

    onError: (error) => {
      message.error(error.response?.data?.message || t('ui.corporate.contract_update_failed', 'Failed to update contract'));
    },
  });

  const updatePricesMutation = useMutation({
    mutationFn: ({ contractId, prices }) => adminService.updateCorporateContractPrices(contractId, prices),

    onSuccess: () => {
      message.success(t('ui.corporate.prices_updated', 'Contract prices updated'));
      setIsPricesDrawerOpen(false);
      invalidateCorporateQueries();
    },

    onError: (error) => {
      message.error(error.response?.data?.message || t('ui.corporate.prices_update_failed', 'Failed to update prices'));
    },
  });

  const topupMutation = useMutation({
    mutationFn: ({ contractId, payload }) => adminService.topupCorporateContract(contractId, payload),

    onSuccess: () => {
      message.success(t('ui.corporate.topup_success', 'Prepayment topup applied'));
      setIsTopupModalOpen(false);
      topupForm.resetFields();
      invalidateCorporateQueries();
    },

    onError: (error) => {
      message.error(error.response?.data?.message || t('ui.corporate.topup_failed', 'Failed to apply topup'));
    },
  });

  const previewContractOverlapMutation = useMutation({
    mutationFn: (payload) => adminService.previewCorporateContractOverlaps(payload),

    onSuccess: (response) => {
      setContractOverlapPreview(response?.data?.preview || null);
    },

    onError: (error) => {
      setContractOverlapPreview(null);
      message.error(error.response?.data?.message || t('ui.corporate.overlap_preview_failed', 'Failed to preview overlaps'));
    },
  });

  const previewPricesOverlapMutation = useMutation({
    mutationFn: (payload) => adminService.previewCorporateContractOverlaps(payload),

    onSuccess: (response) => {
      setPricesOverlapPreview(response?.data?.preview || null);
    },

    onError: (error) => {
      setPricesOverlapPreview(null);
      message.error(error.response?.data?.message || t('ui.corporate.overlap_preview_failed', 'Failed to preview overlaps'));
    },
  });

  const handleOpenCreateModal = () => {
    setEditingContract(null);
    setContractOverlapPreview(null);
    contractForm.resetFields();
    contractForm.setFieldsValue({
      status: 'active',
      currency: 'UZS',
      is_active: true,
      is_loyalty_points_eligible: false,
      allows_debt: false,
    });
    setIsContractModalOpen(true);
  };

  const handleOpenEditModal = () => {
    if (!selectedContract) {
      return;
    }

    setEditingContract(selectedContract);
    setContractOverlapPreview(null);
    contractForm.setFieldsValue({
      user_id: selectedContract.user_id,
      contract_number: selectedContract.contract_number,
      name: selectedContract.name,
      status: selectedContract.status,
      start_date: selectedContract.start_date ? selectedContract.start_date.split('T')[0] : '',
      end_date: selectedContract.end_date ? selectedContract.end_date.split('T')[0] : '',
      currency: selectedContract.currency || 'UZS',
      notes: selectedContract.notes || '',
      is_active: selectedContract.is_active !== false,
      is_loyalty_points_eligible: selectedContract.is_loyalty_points_eligible === true,
      allows_debt: selectedContract.allows_debt === true,
      account_number: selectedContract.bank_details?.account_number || '',
      bank_name: selectedContract.bank_details?.bank_name || '',
      mfo: selectedContract.bank_details?.mfo || '',
      inn: selectedContract.bank_details?.inn || ''
    });
    setIsContractModalOpen(true);
  };

  const handleContractSubmit = (values) => {
    const payload = buildContractPayload(values);
    if (editingContract?.id) {
      updateContractMutation.mutate({ contractId: editingContract.id, payload });
      return;
    }
    createContractMutation.mutate(payload);
  };

  const handleOpenPricesDrawer = () => {
    if (!selectedContract) {
      return;
    }

    setPricesOverlapPreview(null);
    pricesForm.setFieldsValue({
      prices: selectedContract.prices?.length
        ? selectedContract.prices.map((price) => ({
            product_id: price.product_id,
            unit_price: price.unit_price,
            is_prepayment_eligible: price.is_prepayment_eligible !== false,
            is_active: price.is_active !== false,
            notes: price.notes || ''
          }))
        : [
            {
              is_prepayment_eligible: true,
              is_active: true
            }
          ]
    });
    setIsPricesDrawerOpen(true);
  };

  const handlePricesSubmit = (values) => {
    const priceRows = (values.prices || [])
      .filter((row) => row && row.product_id && row.unit_price !== undefined && row.unit_price !== null)
      .map((row) => ({
        product_id: row.product_id,
        unit_price: row.unit_price,
        is_prepayment_eligible: row.is_prepayment_eligible !== false,
        is_active: row.is_active !== false,
        notes: row.notes || ''
      }));

    updatePricesMutation.mutate({
      contractId: selectedContract.id,
      prices: priceRows
    });
  };

  const handleTopupSubmit = (values) => {
    topupMutation.mutate({
      contractId: selectedContract.id,
      payload: {
        product_id: values.product_id,
        units: values.units,
        amount: values.amount,
        transfer_ref: values.transfer_ref,
        notes: values.notes
      }
    });
  };

  const renderOverlapPreview = (preview) => {
    if (!preview) {
      return null;
    }

    if (!preview.has_conflicts) {
      return (
        <Alert
          type="success"
          showIcon
          message={t('ui.corporate.no_overlap_conflicts', 'No overlapping active contract coverage found for the current selection.')}
        />
      );
    }

    return (
      <Alert
        type="warning"
        showIcon
        message={t('ui.corporate.overlap_conflicts_found', 'Overlap conflicts found')}
        description={(
          <div>
            <div style={{ marginBottom: 8 }}>
              {t(
                'ui.corporate.overlap_conflicts_summary',
                '{{count}} conflicts across {{products}} product(s) and {{contracts}} contract(s).',
                {
                  count: preview.summary?.conflicts_count || 0,
                  products: preview.summary?.products_count || 0,
                  contracts: preview.summary?.conflicting_contracts_count || 0,
                }
              )}
            </div>
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {(preview.conflicts || []).map((conflict, index) => (
                <li key={`${conflict.product_id}-${conflict.conflicting_contract?.id || index}`}>
                  {`${conflict.product_name || `#${conflict.product_id}`}: ${conflict.conflicting_contract?.contract_number || '-'}`}
                </li>
              ))}
            </ul>
          </div>
        )}
      />
    );
  };

  const buildContractOverlapPayload = () => {
    const values = contractForm.getFieldsValue();
    return {
      contract_id: editingContract?.id,
      user_id: values.user_id,
      contract_number: values.contract_number,
      name: values.name,
      status: values.status,
      start_date: values.start_date || undefined,
      end_date: values.end_date || undefined,
      is_active: values.is_active !== false,
      prices: (selectedContract?.prices || []).map((price) => ({
        product_id: price.product_id,
        is_active: price.is_active !== false,
      })),
    };
  };

  const buildPricesOverlapPayload = () => {
    const values = pricesForm.getFieldsValue();
    return {
      contract_id: selectedContract?.id,
      user_id: selectedContract?.user_id,
      contract_number: selectedContract?.contract_number,
      name: selectedContract?.name,
      status: selectedContract?.status,
      start_date: selectedContract?.start_date,
      end_date: selectedContract?.end_date,
      is_active: selectedContract?.is_active !== false,
      prices: (values.prices || []).map((row) => ({
        product_id: row?.product_id,
        is_active: row?.is_active !== false,
      })),
    };
  };

  const handlePreviewContractOverlap = () => {
    previewContractOverlapMutation.mutate(buildContractOverlapPayload());
  };

  const handlePreviewPricesOverlap = () => {
    previewPricesOverlapMutation.mutate(buildPricesOverlapPayload());
  };

  const contractColumns = [
    {
      title: t('ui.corporate.contract_number', 'Contract'),
      dataIndex: 'contract_number',
      key: 'contract_number',
      render: (value) => <span style={{ fontFamily: 'monospace', fontWeight: 700 }}>{value}</span>
    },
    {
      title: t('ui.corporate.customer', 'Customer'),
      key: 'user',
      render: (_, record) => (
        <div>
          <div>{`${record.user?.first_name || ''} ${record.user?.last_name || ''}`.trim() || t('ui.corporate.unknown_customer', 'Unknown')}</div>
          <small style={{ color: '#666' }}>{record.user?.phone || record.user?.email || ''}</small>
        </div>
      )
    },
    {
      title: t('ui.corporate.status', 'Status'),
      dataIndex: 'status',
      key: 'status',
      render: (status) => (
        // eslint-disable-next-line security/detect-object-injection
        <Tag color={statusColors[status] || 'default'}>
          {status}
        </Tag>
      )
    },
    {
      title: t('ui.corporate.loyalty_points', 'Loyalty'),
      dataIndex: 'is_loyalty_points_eligible',
      key: 'is_loyalty_points_eligible',
      render: (value) => (
        <Tag color={value ? 'green' : 'default'}>
          {value ? t('ui.corporate.loyalty_eligible', 'Eligible') : t('ui.corporate.loyalty_ineligible', 'Not eligible')}
        </Tag>
      )
    },
    {
      title: t('ui.corporate.debt_policy', 'Debt'),
      dataIndex: 'allows_debt',
      key: 'allows_debt',
      render: (value) => (
        <Tag color={value ? 'orange' : 'default'}>
          {value ? t('ui.corporate.debt_allowed', 'Debt allowed') : t('ui.corporate.debt_disallowed', 'No debt')}
        </Tag>
      )
    },
    {
      title: t('ui.corporate.prepayment_scope', 'Prepayment Scope'),
      key: 'prepayment_scope',
      render: (_, record) => {
        const tracked = Number(record.prepayment_account?.tracked_products_count || 0);
        const debt = Number(record.prepayment_account?.debt_products_count || 0);
        return `${tracked} tracked / ${debt} debt`;
      }
    }
  ];

  const ledgerColumns = [
    {
      title: t('ui.corporate.event_date', 'Created'),
      dataIndex: 'created_at',
      key: 'created_at',
      render: (value) => formatDateTimeShort(value)
    },
    {
      title: t('ui.corporate.product', 'Product'),
      key: 'product',
      render: (_, record) => record.product_name || `#${record.product_id || '-'}`,
      ellipsis: true
    },
    {
      title: t('ui.corporate.event_type', 'Event'),
      dataIndex: 'event_type',
      key: 'event_type',
      render: (value) => <Tag>{value}</Tag>
    },
    {
      title: t('ui.corporate.units', 'Units'),
      dataIndex: 'units',
      key: 'units',
      render: (value) => formatUnits(value)
    },
    {
      title: t('ui.corporate.unit_price', 'Unit Price'),
      dataIndex: 'unit_price_snapshot',
      key: 'unit_price_snapshot',
      render: (value) => (value !== null && value !== undefined ? `${formatAmount(value)} ${selectedContract?.currency || 'UZS'}` : '-')
    },
    {
      title: t('ui.corporate.amount', 'Amount'),
      dataIndex: 'amount',
      key: 'amount',
      render: (value) => (value !== null && value !== undefined ? `${formatAmount(value)} ${selectedContract?.currency || 'UZS'}` : '-')
    },
    {
      title: t('ui.corporate.reference', 'Reference'),
      dataIndex: 'transfer_reference',
      key: 'transfer_reference',
      render: (value) => value || '-'
    },
    {
      title: t('ui.corporate.notes', 'Notes'),
      dataIndex: 'notes',
      key: 'notes',
      ellipsis: true
    }
  ];

  const totalContracts = contracts.length;
  const activeContracts = contracts.filter((contract) => contract.status === 'active' && contract.is_active).length;
  const contractsWithDebt = contracts.filter(
    (contract) => Number(contract.prepayment_account?.debt_products_count || 0) > 0
  ).length;
  const topupEligibleProducts = (selectedContract?.prices || []).filter(
    (price) => price.is_active !== false && price.is_prepayment_eligible !== false
  );

  return (
    <div>
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title={t('ui.corporate.total_contracts', 'Total Contracts')}
              value={totalContracts}
              prefix={<BankOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title={t('ui.corporate.active_contracts', 'Active Contracts')}
              value={activeContracts}
              valueStyle={{ color: '#3f8600' }}
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title={t('ui.corporate.contracts_with_debt', 'Contracts With Debt')}
              value={contractsWithDebt}
              prefix={<DollarCircleOutlined />}
            />
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col xs={24} xl={10}>
          <Card
            title={t('ui.corporate.contracts', 'Corporate Contracts')}
            extra={(
              <Space>
                <Button icon={<ReloadOutlined />} onClick={() => contractsQuery.refetch()} />
                <Button type="primary" icon={<PlusOutlined />} onClick={handleOpenCreateModal}>
                  {t('ui.corporate.new_contract', 'New Contract')}
                </Button>
              </Space>
            )}
          >
            <Search
              placeholder={t('ui.corporate.search_contracts', 'Search by contract, customer, phone')}
              allowClear
              onSearch={setSearchText}
              onChange={(event) => setSearchText(event.target.value)}
              style={{ marginBottom: 16 }}
            />

            <Table
              rowKey="id"
              loading={contractsQuery.isLoading}
              columns={contractColumns}
              dataSource={visibleContracts}
              pagination={false}
              onRow={(record) => ({
                onClick: () => setSelectedContractId(record.id),
                style: {
                  cursor: 'pointer',
                  backgroundColor: record.id === selectedContractId ? '#f0f7ff' : undefined
                }
              })}
            />
          </Card>
        </Col>

        <Col xs={24} xl={14}>
          <Card
            title={selectedContract?.name || t('ui.corporate.contract_details', 'Contract Details')}
            loading={contractDetailQuery.isLoading}
            extra={selectedContract ? (
              <Space>
                <Button onClick={handleOpenEditModal}>
                  {t('ui.common.edit', 'Edit')}
                </Button>
                <Button onClick={handleOpenPricesDrawer}>
                  {t('ui.corporate.manage_prices', 'Manage Prices')}
                </Button>
                <Button
                  type="primary"
                  disabled={topupEligibleProducts.length === 0}
                  onClick={() => {
                    topupForm.setFieldsValue({
                      product_id: topupEligibleProducts[0]?.product_id,
                      units: 1,
                    });
                    setIsTopupModalOpen(true);
                  }}
                >
                  {t('ui.corporate.topup', 'Top Up')}
                </Button>
              </Space>
            ) : null}
          >
            {!selectedContract ? (
              <div style={{ color: '#666' }}>
                {t('ui.corporate.select_contract_hint', 'Select a contract to view pricing, balances, and ledger entries.')}
              </div>
            ) : (
              <>
                <Descriptions bordered column={2} size="small">
                  <Descriptions.Item label={t('ui.corporate.contract_number', 'Contract')}>
                    {selectedContract.contract_number}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('ui.corporate.status', 'Status')}>
                    <Tag color={statusColors[selectedContract.status] || 'default'}>
                      {selectedContract.status}
                    </Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label={t('ui.corporate.customer', 'Customer')}>
                    {`${selectedContract.user?.first_name || ''} ${selectedContract.user?.last_name || ''}`.trim()}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('ui.corporate.phone', 'Phone')}>
                    {selectedContract.user?.phone || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('ui.corporate.currency', 'Currency')}>
                    {selectedContract.currency}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('ui.corporate.period', 'Period')}>
                    {selectedContract.start_date ? selectedContract.start_date.split('T')[0] : '-'}
                    {' - '}
                    {selectedContract.end_date ? selectedContract.end_date.split('T')[0] : t('ui.corporate.open_ended', 'Open ended')}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('ui.corporate.bank', 'Bank')}>
                    {selectedContract.bank_details?.bank_name || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('ui.corporate.account', 'Account')}>
                    {selectedContract.bank_details?.account_number || '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('ui.corporate.loyalty_points', 'Loyalty Points')}>
                    <Tag color={selectedContract.is_loyalty_points_eligible ? 'green' : 'default'}>
                      {selectedContract.is_loyalty_points_eligible
                        ? t('ui.corporate.loyalty_eligible', 'Eligible')
                        : t('ui.corporate.loyalty_ineligible', 'Not eligible')}
                    </Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label={t('ui.corporate.debt_policy', 'Debt Policy')}>
                    <Tag color={selectedContract.allows_debt ? 'orange' : 'default'}>
                      {selectedContract.allows_debt
                        ? t('ui.corporate.debt_allowed', 'Debt allowed')
                        : t('ui.corporate.debt_disallowed', 'No debt')}
                    </Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label={t('ui.corporate.tracking_mode', 'Tracking Mode')}>
                    <Tag color={selectedContract.tracking_mode === 'amount' ? 'orange' : 'blue'}>
                      {selectedContract.tracking_mode === 'amount'
                        ? t('ui.corporate.tracking_mode_amount', 'Money (Grocery Store)')
                        : t('ui.corporate.tracking_mode_units', 'Units (Workplace)')}
                    </Tag>
                  </Descriptions.Item>
                </Descriptions>

                <Divider>{t('ui.corporate.balance', 'Balance')}</Divider>

                {selectedContract.tracking_mode === 'amount' ? (
                  <Row gutter={[12, 12]}>
                    <Col xs={24} md={8}>
                      <Statistic
                        title={t('ui.corporate.outstanding_amount', 'Outstanding (debt)')}
                        value={balanceSummary.outstanding_amount || 0}
                        precision={2}
                        suffix={selectedContract.currency}
                        valueStyle={{
                          color: Number(balanceSummary.outstanding_amount || 0) > 0
                            ? '#cf1322'
                            : Number(balanceSummary.outstanding_amount || 0) < 0
                            ? '#3f8600'
                            : undefined
                        }}
                      />
                    </Col>
                    <Col xs={12} md={8}>
                      <Statistic
                        title={t('ui.corporate.lifetime_charged', 'Lifetime Charged')}
                        value={balanceSummary.lifetime_charged || 0}
                        precision={2}
                        suffix={selectedContract.currency}
                      />
                    </Col>
                    <Col xs={12} md={8}>
                      <Statistic
                        title={t('ui.corporate.lifetime_collected', 'Lifetime Collected')}
                        value={balanceSummary.lifetime_collected || 0}
                        precision={2}
                        suffix={selectedContract.currency}
                      />
                    </Col>
                    <Col xs={12} md={8}>
                      <Statistic
                        title={t('ui.corporate.last_charged', 'Last Charged')}
                        value={balanceSummary.last_charged_at ? formatDateTimeShort(balanceSummary.last_charged_at) : '-'}
                      />
                    </Col>
                    <Col xs={12} md={8}>
                      <Statistic
                        title={t('ui.corporate.last_collected', 'Last Collected')}
                        value={balanceSummary.last_collected_at ? formatDateTimeShort(balanceSummary.last_collected_at) : '-'}
                      />
                    </Col>
                  </Row>
                ) : (
                  <Row gutter={[12, 12]}>
                    <Col xs={12} md={8}>
                      <Statistic title={t('ui.corporate.tracked_products', 'Tracked Products')} value={balanceSummary.tracked_products_count || 0} />
                    </Col>
                    <Col xs={12} md={8}>
                      <Statistic title={t('ui.corporate.products_reserved', 'Reserved Products')} value={balanceSummary.products_with_reservations_count || 0} />
                    </Col>
                    <Col xs={12} md={8}>
                      <Statistic title={t('ui.corporate.products_in_debt', 'Products In Debt')} value={balanceSummary.products_in_debt_count || 0} valueStyle={{ color: '#cf1322' }} />
                    </Col>
                    <Col xs={12} md={8}>
                      <Statistic title={t('ui.corporate.last_topup', 'Last Topup')} value={balanceSummary.last_topup_at ? formatDateTimeShort(balanceSummary.last_topup_at) : '-'} />
                    </Col>
                  </Row>
                )}

                {selectedContract.tracking_mode !== 'amount' && (
                <Table
                  rowKey={(record) => `balance-${record.product_id}`}
                  dataSource={balanceProducts}
                  pagination={false}
                  size="small"
                  style={{ marginTop: 16 }}
                  columns={[
                    {
                      title: t('ui.corporate.product', 'Product'),
                      key: 'product',
                      render: (_, record) => (
                        <div>
                          <div>{record.product_name || `#${record.product_id}`}</div>
                          <small style={{ color: '#666' }}>{record.product_size || ''}</small>
                        </div>
                      )
                    },
                    {
                      title: t('ui.corporate.unit_price', 'Unit Price'),
                      dataIndex: 'contract_unit_price',
                      key: 'contract_unit_price',
                      render: (value) => (value !== null && value !== undefined ? `${formatAmount(value)} ${selectedContract.currency}` : '-')
                    },
                    {
                      title: t('ui.corporate.prepaid', 'Prepaid'),
                      dataIndex: 'prepaid_units',
                      key: 'prepaid_units',
                      render: formatUnits
                    },
                    {
                      title: t('ui.corporate.reserved', 'Reserved'),
                      dataIndex: 'reserved_units',
                      key: 'reserved_units',
                      render: formatUnits
                    },
                    {
                      title: t('ui.corporate.consumed', 'Consumed'),
                      dataIndex: 'consumed_units',
                      key: 'consumed_units',
                      render: formatUnits
                    },
                    {
                      title: t('ui.corporate.available', 'Available'),
                      dataIndex: 'available_units',
                      key: 'available_units',
                      render: (value) => (
                        <span style={{ color: Number(value) < 0 ? '#cf1322' : undefined }}>
                          {formatUnits(value)}
                        </span>
                      )
                    },
                    {
                      title: t('ui.corporate.debt', 'Debt'),
                      dataIndex: 'debt_units',
                      key: 'debt_units',
                      render: (value) => (
                        <span style={{ color: Number(value) > 0 ? '#cf1322' : undefined }}>
                          {formatUnits(value)}
                        </span>
                      )
                    }
                  ]}
                  locale={{
                    emptyText: t('ui.corporate.no_balance_products', 'No prepayment-eligible products configured yet.')
                  }}
                />
                )}

                <Divider>{t('ui.corporate.price_overrides', 'Price Overrides')}</Divider>

                <Table
                  rowKey={(record) => `${record.product_id}-${record.id || 'new'}`}
                  dataSource={selectedContract.prices || []}
                  pagination={false}
                  size="small"
                  columns={[
                    {
                      title: t('ui.corporate.product', 'Product'),
                      key: 'product',
                      render: (_, record) => (
                        <div>
                          <div>{record.product_name || `#${record.product_id}`}</div>
                          <small style={{ color: '#666' }}>{record.product_size || ''}</small>
                        </div>
                      )
                    },
                    {
                      title: t('ui.corporate.unit_price', 'Unit Price'),
                      dataIndex: 'unit_price',
                      key: 'unit_price',
                      render: (value) => `${formatAmount(value)} ${selectedContract.currency}`
                    },
                    {
                      title: t('ui.corporate.prepayment_eligible', 'Prepayment'),
                      dataIndex: 'is_prepayment_eligible',
                      key: 'is_prepayment_eligible',
                      render: (value) => <Tag color={value ? 'green' : 'default'}>{value ? 'Yes' : 'No'}</Tag>
                    },
                    {
                      title: t('ui.common.status', 'Status'),
                      dataIndex: 'is_active',
                      key: 'is_active',
                      render: (value) => <Tag color={value ? 'green' : 'default'}>{value ? 'Active' : 'Inactive'}</Tag>
                    }
                  ]}
                  locale={{
                    emptyText: t('ui.corporate.no_price_overrides', 'No contract-specific price overrides yet.')
                  }}
                />

                <Divider>{t('ui.corporate.ledger', 'Ledger')}</Divider>

                <Table
                  rowKey="id"
                  loading={contractLedgerQuery.isLoading}
                  dataSource={ledgerItems}
                  columns={ledgerColumns}
                  size="small"
                  pagination={{ pageSize: 8 }}
                  locale={{
                    emptyText: t('ui.corporate.no_ledger_entries', 'No ledger entries yet.')
                  }}
                />
              </>
            )}
          </Card>
        </Col>
      </Row>

      <Modal
        title={editingContract ? t('ui.corporate.edit_contract', 'Edit Contract') : t('ui.corporate.new_contract', 'New Contract')}
        open={isContractModalOpen}
        onCancel={() => {
          setIsContractModalOpen(false);
          setEditingContract(null);
          setContractOverlapPreview(null);
          contractForm.resetFields();
        }}
        onOk={() => contractForm.submit()}
        confirmLoading={createContractMutation.isPending || updateContractMutation.isPending}
        width={760}
      >
        <Form form={contractForm} layout="vertical" onFinish={handleContractSubmit}>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="user_id"
                label={t('ui.corporate.customer', 'Customer')}
                rules={[{ required: true, message: t('ui.corporate.customer_required', 'Customer is required') }]}
              >
                <Select
                  showSearch
                  placeholder={t('ui.corporate.select_customer', 'Select customer')}
                  optionFilterProp="children"
                >
                  {corporateUsers.map((user) => (
                    <Option key={user.id} value={user.id}>
                      {user.company_name || `${user.first_name || ''} ${user.last_name || ''}`.trim() || user.email || user.id}
                      {` - ${user.phone || user.email || user.id}`}
                    </Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="contract_number"
                label={t('ui.corporate.contract_number', 'Contract Number')}
                rules={[{ required: true, message: t('ui.corporate.contract_number_required', 'Contract number is required') }]}
              >
                <Input />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="name"
                label={t('ui.corporate.contract_name', 'Contract Name')}
                rules={[{ required: true, message: t('ui.corporate.contract_name_required', 'Contract name is required') }]}
              >
                <Input />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name="status" label={t('ui.corporate.status', 'Status')}>
                <Select>
                  <Option value="draft">draft</Option>
                  <Option value="active">active</Option>
                  <Option value="suspended">suspended</Option>
                  <Option value="terminated">terminated</Option>
                </Select>
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name="currency" label={t('ui.corporate.currency', 'Currency')}>
                <Select>
                  <Option value="UZS">UZS</Option>
                </Select>
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="start_date" label={t('ui.corporate.start_date', 'Start Date')}>
                <Input type="date" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="end_date" label={t('ui.corporate.end_date', 'End Date')}>
                <Input type="date" />
              </Form.Item>
            </Col>
          </Row>

          <Divider>{t('ui.corporate.bank_details', 'Bank Details')}</Divider>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="bank_name" label={t('ui.corporate.bank', 'Bank')}>
                <Input />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="account_number" label={t('ui.corporate.account', 'Account Number')}>
                <Input />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="mfo" label={t('ui.corporate.mfo', 'MFO')}>
                <Input />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="inn" label={t('ui.corporate.inn', 'INN')}>
                <Input />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item name="notes" label={t('ui.corporate.notes', 'Notes')}>
            <TextArea rows={3} />
          </Form.Item>

          <Form.Item name="is_active" valuePropName="checked">
            <Checkbox>{t('ui.corporate.is_active', 'Contract is active')}</Checkbox>
          </Form.Item>

          <Form.Item name="is_loyalty_points_eligible" valuePropName="checked">
            <Checkbox>{t('ui.corporate.loyalty_points_eligible', 'Eligible for loyalty points')}</Checkbox>
          </Form.Item>

          <Form.Item name="allows_debt" valuePropName="checked">
            <Checkbox>{t('ui.corporate.allows_debt', 'Allow contract debt')}</Checkbox>
          </Form.Item>

          <div style={{ marginBottom: 16 }}>
            <Button onClick={handlePreviewContractOverlap} loading={previewContractOverlapMutation.isPending}>
              {t('ui.corporate.preview_overlaps', 'Preview Overlaps')}
            </Button>
          </div>

          {renderOverlapPreview(contractOverlapPreview)}
        </Form>
      </Modal>

      <Modal
        title={t('ui.corporate.topup', 'Top Up Prepayment')}
        open={isTopupModalOpen}
        onCancel={() => {
          setIsTopupModalOpen(false);
          topupForm.resetFields();
        }}
        onOk={() => topupForm.submit()}
        confirmLoading={topupMutation.isPending}
        >
        <Form
          form={topupForm}
          layout="vertical"
          onFinish={handleTopupSubmit}
          initialValues={{ units: 1 }}
        >
          <Form.Item
            name="product_id"
            label={t('ui.corporate.product', 'Product')}
            rules={[{ required: true, message: t('ui.corporate.product_required', 'Product is required') }]}
          >
            <Select placeholder={t('ui.corporate.select_product', 'Select product')}>
              {topupEligibleProducts.map((price) => (
                <Option key={price.product_id} value={price.product_id}>
                  {price.product_name || `#${price.product_id}`}
                  {price.product_size ? ` (${price.product_size})` : ''}
                </Option>
              ))}
            </Select>
          </Form.Item>
          <Form.Item
            name="units"
            label={t('ui.corporate.units', 'Units')}
            rules={[{ required: true, message: t('ui.corporate.units_required', 'Units are required') }]}
          >
            <InputNumber min={0.01} step={1} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="amount" label={t('ui.corporate.amount', 'Amount')}>
            <InputNumber min={0} step={1000} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="transfer_ref" label={t('ui.corporate.reference', 'Transfer Reference')}>
            <Input />
          </Form.Item>
          <Form.Item name="notes" label={t('ui.corporate.notes', 'Notes')}>
            <TextArea rows={3} />
          </Form.Item>
        </Form>
      </Modal>

      <Drawer
        title={t('ui.corporate.manage_prices', 'Manage Contract Prices')}
        open={isPricesDrawerOpen}
        onClose={() => {
          setIsPricesDrawerOpen(false);
          setPricesOverlapPreview(null);
        }}
        width={720}
        extra={(
          <Space>
            <Button onClick={handlePreviewPricesOverlap} loading={previewPricesOverlapMutation.isPending}>
              {t('ui.corporate.preview_overlaps', 'Preview Overlaps')}
            </Button>
            <Button
              type="primary"
              onClick={() => pricesForm.submit()}
              loading={updatePricesMutation.isPending}
            >
              {t('ui.common.save', 'Save')}
            </Button>
          </Space>
        )}
      >
        <div style={{ marginBottom: 16 }}>
          {renderOverlapPreview(pricesOverlapPreview)}
        </div>
        <Form form={pricesForm} layout="vertical" onFinish={handlePricesSubmit}>
          <Form.List name="prices">
            {(fields, { add, remove }) => (
              <>
                {fields.map((field) => (
                  <Card
                    key={field.key}
                    size="small"
                    style={{ marginBottom: 12 }}
                    title={`${t('ui.corporate.price_override', 'Price Override')} #${field.name + 1}`}
                    extra={(
                      <Button type="link" danger onClick={() => remove(field.name)}>
                        {t('ui.common.remove', 'Remove')}
                      </Button>
                    )}
                  >
                    <Row gutter={16}>
                      <Col span={14}>
                        <Form.Item
                          {...field}
                          name={[field.name, 'product_id']}
                          label={t('ui.corporate.product', 'Product')}
                          rules={[{ required: true, message: t('ui.corporate.product_required', 'Product is required') }]}
                        >
                          <Select showSearch optionFilterProp="children">
                            {availableProducts.map((product) => (
                              <Option key={product.id} value={product.id}>
                                {product.name}
                              </Option>
                            ))}
                          </Select>
                        </Form.Item>
                      </Col>
                      <Col span={10}>
                        <Form.Item
                          {...field}
                          name={[field.name, 'unit_price']}
                          label={t('ui.corporate.unit_price', 'Unit Price')}
                          rules={[{ required: true, message: t('ui.corporate.unit_price_required', 'Unit price is required') }]}
                        >
                          <InputNumber min={0} step={100} style={{ width: '100%' }} />
                        </Form.Item>
                      </Col>
                    </Row>

                    <Row gutter={16}>
                      <Col span={12}>
                        <Form.Item
                          {...field}
                          name={[field.name, 'is_prepayment_eligible']}
                          valuePropName="checked"
                        >
                          <Checkbox>{t('ui.corporate.prepayment_eligible', 'Prepayment eligible')}</Checkbox>
                        </Form.Item>
                      </Col>
                      <Col span={12}>
                        <Form.Item
                          {...field}
                          name={[field.name, 'is_active']}
                          valuePropName="checked"
                        >
                          <Checkbox>{t('ui.corporate.is_active', 'Active')}</Checkbox>
                        </Form.Item>
                      </Col>
                    </Row>

                    <Form.Item
                      {...field}
                      name={[field.name, 'notes']}
                      label={t('ui.corporate.notes', 'Notes')}
                    >
                      <Input />
                    </Form.Item>
                  </Card>
                ))}

                <Button
                  type="dashed"
                  block
                  icon={<PlusOutlined />}
                  onClick={() => add({ is_prepayment_eligible: true, is_active: true })}
                >
                  {t('ui.corporate.add_price_override', 'Add Price Override')}
                </Button>
              </>
            )}
          </Form.List>
        </Form>
      </Drawer>
    </div>
  );
};

export default CorporateContracts;
