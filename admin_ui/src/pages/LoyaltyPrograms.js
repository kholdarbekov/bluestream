import React, { useEffect, useMemo, useState } from 'react';
import { DEFAULT_PAGE_SIZE } from '../utils/constants';
import {
  Button,
  Card,
  Col,
  Divider,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  message
} from 'antd';
import {
  DeleteOutlined,
  EditOutlined,
  ExportOutlined,
  GiftOutlined,
  PlusOutlined,
  SettingOutlined,
  StarOutlined,
  TrophyOutlined
} from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import adminService from '../services/adminService';
import exportUtils from '../utils/exportUtils';
import { formatDate } from '../utils/dateUtils';
import AsyncButton from '../components/common/AsyncButton';
import EmptyState from '../components/common/EmptyState';

const { TextArea } = Input;

const LoyaltyPrograms = () => {
  const { t } = useTranslation('loyalty');
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState('programs');
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState();
  const [selectedProgramId, setSelectedProgramId] = useState();
  const [pagination, setPagination] = useState({ page: 1, per_page: DEFAULT_PAGE_SIZE });
  const [programModal, setProgramModal] = useState({ open: false, program: null });
  const [tierModal, setTierModal] = useState({ open: false, tier: null });
  const [streakModal, setStreakModal] = useState({ open: false, rule: null });
  const [consecModal, setConsecModal] = useState({ open: false, rule: null });
  const [programForm] = Form.useForm();
  const [tierForm] = Form.useForm();
  const [streakForm] = Form.useForm();
  const [consecForm] = Form.useForm();

  const programsQuery = useQuery({
    queryKey: ['loyalty-programs', pagination, searchText, statusFilter],

    queryFn: () => adminService.getLoyaltyPrograms({
      page: pagination.page,
      per_page: pagination.per_page,
      search: searchText,
      status: statusFilter,
    }),

    placeholderData: keepPreviousData,
  });

  const programs = programsQuery.data?.items || [];
  const totalPrograms = programsQuery.data?.total || 0;

  useEffect(() => {
    if (!selectedProgramId && programs.length > 0) {
      const defaultProgram = programs.find((program) => program.is_default);
      setSelectedProgramId((defaultProgram || programs[0]).id);
    }
  }, [programs, selectedProgramId]);

  const tiersQuery = useQuery({
    queryKey: ['loyalty-tiers', selectedProgramId],
    queryFn: () => adminService.getLoyaltyTiers({ program_id: selectedProgramId }),
    enabled: Boolean(selectedProgramId),
    placeholderData: keepPreviousData,
  });

  const tiers = tiersQuery.data?.items || [];

  const streakRulesQuery = useQuery({
    queryKey: ['loyalty-streak-rules', selectedProgramId],
    queryFn: () => adminService.getLoyaltyStreakRules({ program_id: selectedProgramId }),
    enabled: Boolean(selectedProgramId),
    placeholderData: keepPreviousData,
  });

  const streakRules = streakRulesQuery.data?.streak_rules || [];

  const consecRulesQuery = useQuery({
    queryKey: ['loyalty-consecutive-strike-rules', selectedProgramId],
    queryFn: () => adminService.getLoyaltyConsecutiveStrikeRules({ program_id: selectedProgramId }),
    enabled: Boolean(selectedProgramId),
    placeholderData: keepPreviousData,
  });
  const consecRules = consecRulesQuery.data?.consecutive_strike_rules || [];

  const invalidateLoyaltyQueries = () => {
    queryClient.invalidateQueries({
      queryKey: ['loyalty-programs'],
    });
    queryClient.invalidateQueries({
      queryKey: ['loyalty-tiers'],
    });
    queryClient.invalidateQueries({
      queryKey: ['loyalty-program-options'],
    });
    queryClient.invalidateQueries({
      queryKey: ['loyalty-streak-rules'],
    });
    queryClient.invalidateQueries({
      queryKey: ['loyalty-consecutive-strike-rules'],
    });
  };

  const createProgramMutation = useMutation({
    mutationFn: (values) => adminService.createLoyaltyProgram(values),

    onSuccess: () => {
      message.success(t('ui.loyalty.create_success', { defaultValue: 'Program created successfully' }));
      setProgramModal({ open: false, program: null });
      programForm.resetFields();
      invalidateLoyaltyQueries();
    },
  });

  const updateProgramMutation = useMutation({
    mutationFn: ({ programId, values }) => adminService.updateLoyaltyProgram(programId, values),

    onSuccess: () => {
      message.success(t('ui.loyalty.update_success', { defaultValue: 'Program updated successfully' }));
      setProgramModal({ open: false, program: null });
      programForm.resetFields();
      invalidateLoyaltyQueries();
    },
  });

  const deleteProgramMutation = useMutation({
    mutationFn: (programId) => adminService.deleteLoyaltyProgram(programId),

    onSuccess: () => {
      message.success(t('ui.loyalty.delete_success', { defaultValue: 'Program updated successfully' }));
      invalidateLoyaltyQueries();
    },
  });

  const createTierMutation = useMutation({
    mutationFn: (values) => adminService.createLoyaltyTier(values),

    onSuccess: () => {
      message.success(t('ui.loyalty.tier_create_success', { defaultValue: 'Tier created successfully' }));
      setTierModal({ open: false, tier: null });
      tierForm.resetFields();
      invalidateLoyaltyQueries();
    },
  });

  const updateTierMutation = useMutation({
    mutationFn: ({ tierId, values }) => adminService.updateLoyaltyTier(tierId, values),

    onSuccess: () => {
      message.success(t('ui.loyalty.tier_update_success', { defaultValue: 'Tier updated successfully' }));
      setTierModal({ open: false, tier: null });
      tierForm.resetFields();
      invalidateLoyaltyQueries();
    },
  });

  const deleteTierMutation = useMutation({
    mutationFn: (tierId) => adminService.deleteLoyaltyTier(tierId),

    onSuccess: () => {
      message.success(t('ui.loyalty.tier_delete_success', { defaultValue: 'Tier removed successfully' }));
      invalidateLoyaltyQueries();
    },
  });

  const createStreakRuleMutation = useMutation({
    mutationFn: (values) => adminService.createLoyaltyStreakRule(values),

    onSuccess: () => {
      message.success(t('ui.loyalty.streak_create_success', { defaultValue: 'Streak rule created successfully' }));
      setStreakModal({ open: false, rule: null });
      streakForm.resetFields();
      invalidateLoyaltyQueries();
    },
  });

  const updateStreakRuleMutation = useMutation({
    mutationFn: ({ ruleId, values }) => adminService.updateLoyaltyStreakRule(ruleId, values),

    onSuccess: () => {
      message.success(t('ui.loyalty.streak_update_success', { defaultValue: 'Streak rule updated successfully' }));
      setStreakModal({ open: false, rule: null });
      streakForm.resetFields();
      invalidateLoyaltyQueries();
    },
  });

  const deleteStreakRuleMutation = useMutation({
    mutationFn: (ruleId) => adminService.deleteLoyaltyStreakRule(ruleId),

    onSuccess: () => {
      message.success(t('ui.loyalty.streak_delete_success', { defaultValue: 'Streak rule removed successfully' }));
      invalidateLoyaltyQueries();
    },
  });

  const createConsecRuleMutation = useMutation({
    mutationFn: (values) => adminService.createLoyaltyConsecutiveStrikeRule(values),
    onSuccess: () => {
      message.success(t('ui.loyalty.consec_create_success', { defaultValue: 'Consecutive-strike rule created' }));
      setConsecModal({ open: false, rule: null });
      consecForm.resetFields();
      invalidateLoyaltyQueries();
    },
  });

  const updateConsecRuleMutation = useMutation({
    mutationFn: ({ ruleId, values }) => adminService.updateLoyaltyConsecutiveStrikeRule(ruleId, values),
    onSuccess: () => {
      message.success(t('ui.loyalty.consec_update_success', { defaultValue: 'Consecutive-strike rule updated' }));
      setConsecModal({ open: false, rule: null });
      consecForm.resetFields();
      invalidateLoyaltyQueries();
    },
  });

  const deleteConsecRuleMutation = useMutation({
    mutationFn: (ruleId) => adminService.deleteLoyaltyConsecutiveStrikeRule(ruleId),
    onSuccess: () => {
      message.success(t('ui.loyalty.consec_delete_success', { defaultValue: 'Consecutive-strike rule removed' }));
      invalidateLoyaltyQueries();
    },
  });

  const handleExport = async () => {
    const result = await exportUtils.exportLoyaltyPrograms({
      search: searchText,
      status: statusFilter,
    });
    if (!result.success) {
      message.error(result.message);
    }
  };

  const programColumns = useMemo(() => ([
    {
      title: t('ui.loyalty.program_name', { defaultValue: 'Program Name' }),
      dataIndex: 'name',
      key: 'name',
      render: (_, record) => (
        <div>
          <div style={{ fontWeight: 600 }}>
            {record.name}
            {record.is_default ? <Tag color="gold" style={{ marginLeft: 8 }}>DEFAULT</Tag> : null}
          </div>
          <div style={{ color: '#8c8c8c' }}>{record.description || '-'}</div>
        </div>
      )
    },
    {
      title: t('ui.loyalty.uzs_per_point', { defaultValue: 'UZS per AquaCoin' }),
      dataIndex: 'uzs_per_point',
      key: 'uzs_per_point',
      width: 140,
    },
    {
      title: t('ui.loyalty.active_members', { defaultValue: 'Members' }),
      dataIndex: 'member_count',
      key: 'member_count',
      width: 120,
    },
    {
      title: t('ui.loyalty.tiers', { defaultValue: 'Tiers' }),
      dataIndex: 'tier_count',
      key: 'tier_count',
      width: 100,
    },
    {
      title: t('ui.loyalty.status', { defaultValue: 'Status' }),
      dataIndex: 'is_active',
      key: 'is_active',
      width: 120,
      render: (value) => <Tag color={value ? 'green' : 'red'}>{value ? 'Active' : 'Inactive'}</Tag>
    },
    {
      title: t('ui.loyalty.created', { defaultValue: 'Created' }),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 140,
      render: (value) => formatDate(value)
    },
    {
      title: t('ui.loyalty.actions', { defaultValue: 'Actions' }),
      key: 'actions',
      width: 120,
      render: (_, record) => (
        <Space>
          <Button
            type="text"
            icon={<EditOutlined />}
            onClick={() => {
              setProgramModal({ open: true, program: record });
              programForm.setFieldsValue(record);
            }}
          />
          <Button
            type="text"
            danger
            icon={<DeleteOutlined />}
            onClick={() => {
              Modal.confirm({
                title: t('ui.loyalty.delete_confirm_title', { defaultValue: 'Delete program?' }),
                content: record.is_default
                  ? t('ui.loyalty.default_program_warning', { defaultValue: 'Default programs cannot be deleted.' })
                  : t('ui.loyalty.delete_confirm_message', { defaultValue: `Delete ${record.name}?` }),
                onOk: () => deleteProgramMutation.mutate(record.id),
                okButtonProps: { disabled: record.is_default },
              });
            }}
          />
        </Space>
      )
    }
  ]), [deleteProgramMutation, programForm, t]);

  const tierColumns = useMemo(() => ([
    {
      title: t('ui.loyalty.tier_name', { defaultValue: 'Tier Name' }),
      dataIndex: 'name',
      key: 'name',
      render: (_, record) => <Tag color={record.color || 'gold'}>{record.name}</Tag>
    },
    {
      title: t('ui.loyalty.points_range', { defaultValue: 'AquaCoins Range' }),
      dataIndex: 'points_range',
      key: 'points_range',
    },
    {
      title: t('ui.loyalty.multiplier', { defaultValue: 'Multiplier' }),
      dataIndex: 'points_multiplier',
      key: 'points_multiplier',
      width: 120,
      render: (value) => `${value || 1}x`
    },
    {
      title: t('ui.loyalty.discount', { defaultValue: 'Discount' }),
      dataIndex: 'discount_percentage',
      key: 'discount_percentage',
      width: 120,
      render: (value) => `${value || 0}%`
    },
    {
      title: t('ui.loyalty.status', { defaultValue: 'Status' }),
      dataIndex: 'is_active',
      key: 'is_active',
      width: 120,
      render: (value) => <Tag color={value ? 'green' : 'red'}>{value ? 'Active' : 'Inactive'}</Tag>
    },
    {
      title: t('ui.loyalty.actions', { defaultValue: 'Actions' }),
      key: 'actions',
      width: 120,
      render: (_, record) => (
        <Space>
          <Button
            type="text"
            icon={<EditOutlined />}
            onClick={() => {
              setTierModal({ open: true, tier: record });
              tierForm.setFieldsValue({
                ...record,
                name_ru: record.translations?.name?.ru,
                name_uz: record.translations?.name?.uz,
              });
            }}
          />
          <Button
            type="text"
            danger
            icon={<DeleteOutlined />}
            onClick={() => {
              Modal.confirm({
                title: t('ui.loyalty.delete_tier_confirm_title', { defaultValue: 'Delete tier?' }),
                content: t('ui.loyalty.delete_tier_confirm_message', { defaultValue: `Delete ${record.name}?` }),
                onOk: () => deleteTierMutation.mutate(record.id),
              });
            }}
          />
        </Space>
      )
    }
  ]), [deleteTierMutation, t, tierForm]);

  const streakRuleColumns = useMemo(() => ([
    {
      title: t('ui.loyalty.streak_name', { defaultValue: 'Name' }),
      dataIndex: 'name',
      key: 'name',
    },
    {
      title: t('ui.loyalty.streak_required_orders', { defaultValue: 'Required Orders' }),
      dataIndex: 'required_orders',
      key: 'required_orders',
      width: 150,
    },
    {
      title: t('ui.loyalty.streak_window_days', { defaultValue: 'Window (days)' }),
      dataIndex: 'window_days',
      key: 'window_days',
      width: 130,
    },
    {
      title: t('ui.loyalty.streak_min_order_amount', { defaultValue: 'Min/order' }),
      dataIndex: 'min_order_amount',
      key: 'min_order_amount',
      width: 120,
      render: (value) => (value != null ? value : '—'),
    },
    {
      title: t('ui.loyalty.streak_bonus_points', { defaultValue: 'Bonus AquaCoins' }),
      dataIndex: 'bonus_points',
      key: 'bonus_points',
      width: 130,
    },
    {
      title: t('ui.loyalty.status', { defaultValue: 'Status' }),
      dataIndex: 'is_active',
      key: 'is_active',
      width: 120,
      render: (value) => <Tag color={value ? 'green' : 'red'}>{value ? 'Active' : 'Inactive'}</Tag>,
    },
    {
      title: t('ui.loyalty.actions', { defaultValue: 'Actions' }),
      key: 'actions',
      width: 120,
      render: (_, record) => (
        <Space>
          <Button
            type="text"
            icon={<EditOutlined />}
            onClick={() => {
              setStreakModal({ open: true, rule: record });
              streakForm.setFieldsValue({
                ...record,
                name_ru: record.translations?.name?.ru,
                name_uz: record.translations?.name?.uz,
              });
            }}
          />
          <Button
            type="text"
            danger
            icon={<DeleteOutlined />}
            onClick={() => {
              Modal.confirm({
                title: t('ui.loyalty.delete_streak_confirm_title', { defaultValue: 'Delete streak rule?' }),
                content: t('ui.loyalty.delete_streak_confirm_message', { defaultValue: `Delete ${record.name}?` }),
                onOk: () => deleteStreakRuleMutation.mutate(record.id),
              });
            }}
          />
        </Space>
      ),
    },
  ]), [deleteStreakRuleMutation, streakForm, t]);

  const consecRuleColumns = useMemo(() => ([
    {
      title: t('ui.loyalty.consec_name', { defaultValue: 'Name' }),
      dataIndex: 'name',
      key: 'name',
    },
    {
      title: t('ui.loyalty.consec_required_consecutive', { defaultValue: 'Required Consecutive' }),
      dataIndex: 'required_consecutive',
      key: 'required_consecutive',
      width: 170,
    },
    {
      title: t('ui.loyalty.consec_combine_mode', { defaultValue: 'Combine Mode' }),
      dataIndex: 'combine_mode',
      key: 'combine_mode',
      width: 130,
      render: (value) => value === 'any' ? 'Any' : 'All',
    },
    {
      title: t('ui.loyalty.consec_strikes', { defaultValue: 'Attached Strikes' }),
      dataIndex: 'strikes',
      key: 'strikes',
      render: (strikes) => (strikes || []).map((s) => <Tag key={s.id}>{s.name}</Tag>),
    },
    {
      title: t('ui.loyalty.streak_bonus_points', { defaultValue: 'Bonus AquaCoins' }),
      dataIndex: 'bonus_points',
      key: 'bonus_points',
      width: 140,
    },
    {
      title: t('ui.loyalty.status', { defaultValue: 'Status' }),
      dataIndex: 'is_active',
      key: 'is_active',
      width: 120,
      render: (value) => <Tag color={value ? 'green' : 'red'}>{value ? 'Active' : 'Inactive'}</Tag>,
    },
    {
      title: t('ui.loyalty.actions', { defaultValue: 'Actions' }),
      key: 'actions',
      width: 120,
      render: (_, record) => (
        <Space>
          <Button
            type="text"
            icon={<EditOutlined />}
            onClick={() => {
              setConsecModal({ open: true, rule: record });
              consecForm.setFieldsValue({
                ...record,
                strike_rule_ids: record.strike_rule_ids,
                name_ru: record.translations?.name?.ru,
                name_uz: record.translations?.name?.uz,
              });
            }}
          />
          <Button
            type="text"
            danger
            icon={<DeleteOutlined />}
            onClick={() => {
              Modal.confirm({
                title: t('ui.loyalty.delete_consec_confirm_title', { defaultValue: 'Delete consecutive-strike rule?' }),
                content: t('ui.loyalty.delete_consec_confirm_message', { defaultValue: `Delete ${record.name}?` }),
                onOk: () => deleteConsecRuleMutation.mutate(record.id),
              });
            }}
          />
        </Space>
      ),
    },
  ]), [consecForm, deleteConsecRuleMutation, t]);

  return (
    <div>
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} md={8}>
          <Card>
            <Statistic
              title={t('ui.loyalty.total_programs', { defaultValue: 'Total Programs' })}
              value={totalPrograms}
              prefix={<GiftOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} md={8}>
          <Card>
            <Statistic
              title={t('ui.loyalty.active_programs', { defaultValue: 'Active Programs' })}
              value={programs.filter((program) => program.is_active).length}
              prefix={<StarOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} md={8}>
          <Card>
            <Statistic
              title={t('ui.loyalty.total_members', { defaultValue: 'Total Members' })}
              value={programs.reduce((sum, program) => sum + (program.member_count || 0), 0)}
              prefix={<TrophyOutlined />}
            />
          </Card>
        </Col>
      </Row>

      <Card>
        <Tabs
          activeKey={activeTab}
          onChange={setActiveTab}
          items={[
            {
              key: 'programs',
              label: t('ui.loyalty.tab_programs', { defaultValue: 'Programs' }),
              children: (
                <>
                  <div className="table-actions">
                    <Space wrap>
                      <Input.Search
                        allowClear
                        placeholder={t('ui.loyalty.search_programs', { defaultValue: 'Search programs' })}
                        style={{ width: 260 }}
                        value={searchText}
                        onChange={(event) => {
                          setSearchText(event.target.value);
                          setPagination((current) => ({ ...current, page: 1 }));
                        }}
                      />
                      <Select
                        allowClear
                        placeholder={t('ui.loyalty.filter_by_status', { defaultValue: 'Filter by status' })}
                        style={{ width: 180 }}
                        value={statusFilter}
                        onChange={(value) => {
                          setStatusFilter(value);
                          setPagination((current) => ({ ...current, page: 1 }));
                        }}
                        options={[
                          { value: 'active', label: 'Active' },
                          { value: 'inactive', label: 'Inactive' },
                        ]}
                      />
                    </Space>

                    <Space>
                      <Button icon={<ExportOutlined />} onClick={handleExport}>
                        {t('ui.loyalty.export_data', { defaultValue: 'Export Programs' })}
                      </Button>
                      <Button
                        type="primary"
                        icon={<PlusOutlined />}
                        onClick={() => {
                          setProgramModal({ open: true, program: null });
                          programForm.resetFields();
                          programForm.setFieldsValue({
                            is_active: true,
                            is_default: false,
                            uzs_per_point: 250,
                            signup_bonus: 100,
                            referral_bonus: 50,
                            birthday_bonus: 25,
                            points_expiry_days: 365,
                            min_redemption_points: 100,
                            surprise_enabled: true,
                            surprise_chance_percent: 5,
                            surprise_amounts: '50,100,200',
                            surprise_cooldown_days: 7,
                            surprise_daily_cap: 5,
                          });
                        }}
                      >
                        {t('ui.loyalty.create_program', { defaultValue: 'Create Program' })}
                      </Button>
                    </Space>
                  </div>

                  <Table
                    rowKey="id"
                    columns={programColumns}
                    dataSource={programs}
                    loading={programsQuery.isLoading}
                    locale={{
                      emptyText: <EmptyState description={t('ui.loyalty.no_programs', { defaultValue: 'No loyalty programs found' })} />
                    }}
                    pagination={{
                      current: pagination.page,
                      pageSize: pagination.per_page,
                      total: totalPrograms,
                      showSizeChanger: true,
                    }}
                    onChange={(pageInfo) => {
                      setPagination({
                        page: pageInfo.current,
                        per_page: pageInfo.pageSize,
                      });
                    }}
                  />
                </>
              )
            },
            {
              key: 'tiers',
              label: t('ui.loyalty.tab_tiers', { defaultValue: 'Tiers' }),
              children: (
                <>
                  <div className="table-actions">
                    <Space wrap>
                      <Select
                        placeholder={t('ui.loyalty.program', { defaultValue: 'Program' })}
                        style={{ width: 240 }}
                        value={selectedProgramId}
                        onChange={setSelectedProgramId}
                        options={programs.map((program) => ({
                          value: program.id,
                          label: program.name,
                        }))}
                      />
                    </Space>
                    <Space>
                      <Button
                        type="primary"
                        icon={<PlusOutlined />}
                        disabled={!selectedProgramId}
                        onClick={() => {
                          setTierModal({ open: true, tier: null });
                          tierForm.resetFields();
                          tierForm.setFieldsValue({
                            program_id: selectedProgramId,
                            is_active: true,
                            display_order: tiers.length,
                            points_multiplier: 1.0,
                            discount_percentage: 0,
                            color: '#CD7F32',
                            icon: 'fa-medal',
                          });
                        }}
                      >
                        {t('ui.loyalty.create_tier', { defaultValue: 'Create Tier' })}
                      </Button>
                    </Space>
                  </div>

                  <Table
                    rowKey="id"
                    columns={tierColumns}
                    dataSource={tiers}
                    loading={tiersQuery.isLoading}
                    locale={{
                      emptyText: <EmptyState description={t('ui.loyalty.no_tiers', { defaultValue: 'No tiers configured' })} />
                    }}
                    pagination={false}
                  />
                </>
              )
            },
            {
              key: 'streak_rules',
              label: t('ui.loyalty.tab_streak_rules', { defaultValue: 'Streak Rules' }),
              children: (
                <>
                  <div className="table-actions">
                    <Space wrap>
                      <Select
                        placeholder={t('ui.loyalty.program', { defaultValue: 'Program' })}
                        style={{ width: 240 }}
                        value={selectedProgramId}
                        onChange={setSelectedProgramId}
                        options={programs.map((program) => ({
                          value: program.id,
                          label: program.name,
                        }))}
                      />
                    </Space>
                    <Space>
                      <Button
                        type="primary"
                        icon={<PlusOutlined />}
                        disabled={!selectedProgramId}
                        onClick={() => {
                          setStreakModal({ open: true, rule: null });
                          streakForm.resetFields();
                          streakForm.setFieldsValue({
                            is_active: true,
                          });
                        }}
                      >
                        {t('ui.loyalty.create_streak_rule', { defaultValue: 'Add Streak Rule' })}
                      </Button>
                    </Space>
                  </div>

                  <Table
                    rowKey="id"
                    columns={streakRuleColumns}
                    dataSource={streakRules}
                    loading={streakRulesQuery.isLoading}
                    locale={{
                      emptyText: <EmptyState description={t('ui.loyalty.no_streak_rules', { defaultValue: 'No streak rules configured' })} />
                    }}
                    pagination={false}
                  />
                </>
              )
            },
            {
              key: 'consecutive_strikes',
              label: t('ui.loyalty.tab_consecutive_strikes', { defaultValue: 'Consecutive Strikes' }),
              children: (
                <>
                  <div className="table-actions">
                    <Space wrap>
                      <Select
                        placeholder={t('ui.loyalty.program', { defaultValue: 'Program' })}
                        style={{ width: 240 }}
                        value={selectedProgramId}
                        onChange={setSelectedProgramId}
                        options={programs.map((program) => ({ value: program.id, label: program.name }))}
                      />
                    </Space>
                    <Space>
                      <Button
                        type="primary"
                        icon={<PlusOutlined />}
                        disabled={!selectedProgramId}
                        onClick={() => {
                          setConsecModal({ open: true, rule: null });
                          consecForm.resetFields();
                          consecForm.setFieldsValue({ is_active: true, combine_mode: 'all' });
                        }}
                      >
                        {t('ui.loyalty.create_consecutive_strike', { defaultValue: 'Add Consecutive Strike' })}
                      </Button>
                    </Space>
                  </div>
                  <Table
                    rowKey="id"
                    columns={consecRuleColumns}
                    dataSource={consecRules}
                    loading={consecRulesQuery.isLoading}
                    locale={{ emptyText: <EmptyState description={t('ui.loyalty.no_consecutive_strikes', { defaultValue: 'No consecutive-strike rules configured' })} /> }}
                    pagination={false}
                  />
                </>
              ),
            }
          ]}
        />
      </Card>

      <Modal
        open={programModal.open}
        title={programModal.program ? t('ui.loyalty.edit_program', { defaultValue: 'Edit Program' }) : t('ui.loyalty.create_program', { defaultValue: 'Create Program' })}
        onCancel={() => setProgramModal({ open: false, program: null })}
        footer={null}
        width={720}
      >
        <Form
          form={programForm}
          layout="vertical"
          onFinish={(values) => {
            const payload = {
              ...values,
              // Mirror `en` from the canonical name so any English surface
              // resolves the program name instead of falling back (uz default).
              translations: { name: { en: values.name } },
            };
            if (programModal.program) {
              updateProgramMutation.mutate({ programId: programModal.program.id, values: payload });
              return;
            }
            createProgramMutation.mutate(payload);
          }}
        >
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="name" label={t('ui.loyalty.program_name', { defaultValue: 'Program Name' })} rules={[{ required: true }]}>
                <Input />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="uzs_per_point" label={t('ui.loyalty.uzs_per_point', { defaultValue: 'UZS per AquaCoin' })} rules={[{ required: true }]}>
                <InputNumber min={1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item name="description" label={t('ui.loyalty.form_description', { defaultValue: 'Description' })}>
            <TextArea rows={3} />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="signup_bonus" label={t('ui.loyalty.signup_bonus', { defaultValue: 'Sign-up Bonus' })}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="referral_bonus" label={t('ui.loyalty.referral_bonus', { defaultValue: 'Referral Bonus' })}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="birthday_bonus" label={t('ui.loyalty.birthday_bonus', { defaultValue: 'Birthday Bonus' })}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="points_expiry_days" label={t('ui.loyalty.points_expiry_days', { defaultValue: 'AquaCoins Expiry Days' })}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="min_redemption_points" label={t('ui.loyalty.min_redemption_points', { defaultValue: 'Minimum Redemption AquaCoins' })}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Space size={24} style={{ marginTop: 30 }}>
                <Form.Item name="is_active" label={t('ui.loyalty.active', { defaultValue: 'Active' })} valuePropName="checked">
                  <Switch />
                </Form.Item>
                <Form.Item name="is_default" label={t('ui.loyalty.default_program', { defaultValue: 'Default Program' })} valuePropName="checked">
                  <Switch />
                </Form.Item>
              </Space>
            </Col>
          </Row>

          <Divider orientation="left">
            {t('ui.loyalty.surprise_section', { defaultValue: 'Surprise Rewards' })}
          </Divider>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="surprise_enabled" label={t('ui.loyalty.surprise_enabled', { defaultValue: 'Surprise Rewards Enabled' })} valuePropName="checked">
                <Switch />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="surprise_chance_percent" label={t('ui.loyalty.surprise_chance', { defaultValue: 'Win Chance (%)' })}>
                <InputNumber min={0} max={100} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="surprise_amounts"
                label={t('ui.loyalty.surprise_amounts', { defaultValue: 'Reward Amounts (comma-separated)' })}
                tooltip={t('ui.loyalty.surprise_amounts_hint', { defaultValue: 'One value is picked at random per win, e.g. 50,100,200' })}
              >
                <Input placeholder="50,100,200" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name="surprise_cooldown_days" label={t('ui.loyalty.surprise_cooldown', { defaultValue: 'Per-user Cooldown (days)' })}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name="surprise_daily_cap" label={t('ui.loyalty.surprise_daily_cap', { defaultValue: 'Global Daily Cap' })}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setProgramModal({ open: false, program: null })}>
                {t('ui.loyalty.cancel', { defaultValue: 'Cancel' })}
              </Button>
              <AsyncButton type="primary" htmlType="submit" loading={createProgramMutation.isPending || updateProgramMutation.isPending}>
                {programModal.program ? t('ui.loyalty.update_program', { defaultValue: 'Update Program' }) : t('ui.loyalty.create_program', { defaultValue: 'Create Program' })}
              </AsyncButton>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={tierModal.open}
        title={tierModal.tier ? t('ui.loyalty.edit_tier', { defaultValue: 'Edit Tier' }) : t('ui.loyalty.create_tier', { defaultValue: 'Create Tier' })}
        onCancel={() => setTierModal({ open: false, tier: null })}
        footer={null}
        width={640}
      >
        <Form
          form={tierForm}
          layout="vertical"
          onFinish={(values) => {
            const payload = {
              ...values,
              program_id: values.program_id || selectedProgramId,
              translations: {
                name: {
                  // Keep `en` in sync with the canonical name field so the page's
                  // get_translated() never falls back to another language for English.
                  en: values.name,
                  ru: values.name_ru || undefined,
                  uz: values.name_uz || undefined,
                },
              },
            };
            if (tierModal.tier) {
              updateTierMutation.mutate({ tierId: tierModal.tier.id, values: payload });
              return;
            }
            createTierMutation.mutate(payload);
          }}
        >
          <Form.Item name="program_id" hidden>
            <InputNumber />
          </Form.Item>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="name" label={t('ui.loyalty.tier_name', { defaultValue: 'Tier Name' })} rules={[{ required: true }]}>
                <Input />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="display_order" label={t('ui.loyalty.display_order', { defaultValue: 'Display Order' })}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="name_ru" label={t('ui.loyalty.name_ru', { defaultValue: 'Name (RU)' })}>
                <Input />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="name_uz" label={t('ui.loyalty.name_uz', { defaultValue: 'Name (UZ)' })}>
                <Input />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="min_points" label={t('ui.loyalty.min_points', { defaultValue: 'Minimum AquaCoins' })} rules={[{ required: true }]}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="max_points" label={t('ui.loyalty.max_points', { defaultValue: 'Maximum AquaCoins' })}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="points_multiplier" label={t('ui.loyalty.multiplier', { defaultValue: 'Multiplier' })}>
                <InputNumber min={1} step={0.1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="discount_percentage" label={t('ui.loyalty.discount', { defaultValue: 'Discount' })}>
                <InputNumber min={0} max={100} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="color" label={t('ui.loyalty.tier_color', { defaultValue: 'Color' })}>
                <Input />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="icon" label={t('ui.loyalty.tier_icon', { defaultValue: 'Icon' })}>
                <Input />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item name="is_active" label={t('ui.loyalty.status', { defaultValue: 'Status' })} valuePropName="checked">
            <Switch />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setTierModal({ open: false, tier: null })}>
                {t('ui.loyalty.cancel', { defaultValue: 'Cancel' })}
              </Button>
              <AsyncButton type="primary" htmlType="submit" loading={createTierMutation.isPending || updateTierMutation.isPending}>
                {tierModal.tier ? t('ui.loyalty.update', { defaultValue: 'Update' }) : t('ui.loyalty.create', { defaultValue: 'Create' })}
              </AsyncButton>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        open={consecModal.open}
        title={consecModal.rule ? t('ui.loyalty.edit_consec_rule', { defaultValue: 'Edit Consecutive-Strike Rule' }) : t('ui.loyalty.create_consec_rule', { defaultValue: 'Add Consecutive-Strike Rule' })}
        onCancel={() => setConsecModal({ open: false, rule: null })}
        footer={null}
        width={640}
      >
        <Form
          form={consecForm}
          layout="vertical"
          onFinish={(values) => {
            const payload = {
              name: values.name,
              required_consecutive: Number(values.required_consecutive),
              combine_mode: values.combine_mode || 'all',
              bonus_points: Number(values.bonus_points),
              strike_rule_ids: values.strike_rule_ids || [],
              is_active: values.is_active,
              program_id: selectedProgramId,
              translations: {
                name: {
                  // Mirror `en` from the canonical name field so the public
                  // /loyalty-guide page resolves English instead of falling
                  // back to another language (DEFAULT_LANGUAGE is uz).
                  en: values.name,
                  ru: values.name_ru || undefined,
                  uz: values.name_uz || undefined,
                },
              },
            };
            if (consecModal.rule) {
              updateConsecRuleMutation.mutate({ ruleId: consecModal.rule.id, values: payload });
              return;
            }
            createConsecRuleMutation.mutate(payload);
          }}
        >
          <Row gutter={16}>
            <Col span={24}>
              <Form.Item name="name" label={t('ui.loyalty.consec_name', { defaultValue: 'Name' })} rules={[{ required: true }]}>
                <Input id="name" />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="name_ru" label={t('ui.loyalty.name_ru', { defaultValue: 'Name (RU)' })}>
                <Input />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="name_uz" label={t('ui.loyalty.name_uz', { defaultValue: 'Name (UZ)' })}>
                <Input />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="required_consecutive" label={t('ui.loyalty.consec_required_consecutive', { defaultValue: 'Required Consecutive' })} rules={[{ required: true }]}>
                <InputNumber id="required_consecutive" min={2} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="bonus_points" label={t('ui.loyalty.streak_bonus_points', { defaultValue: 'Bonus AquaCoins' })} rules={[{ required: true }]}>
                <InputNumber id="bonus_points" min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item name="combine_mode" label={t('ui.loyalty.consec_combine_mode', { defaultValue: 'Combine Mode' })}>
            <Select
              options={[
                { value: 'all', label: t('ui.loyalty.combine_all', { defaultValue: 'All strikes (AND)' }) },
                { value: 'any', label: t('ui.loyalty.combine_any', { defaultValue: 'Any strike (OR)' }) },
              ]}
            />
          </Form.Item>

          <Form.Item name="strike_rule_ids" label={t('ui.loyalty.consec_strikes', { defaultValue: 'Attached Strikes' })} rules={[{ required: true }]}>
            <Select
              mode="multiple"
              options={streakRules.map((s) => ({ value: s.id, label: s.name }))}
            />
          </Form.Item>

          <Form.Item name="is_active" label={t('ui.loyalty.active', { defaultValue: 'Active' })} valuePropName="checked">
            <Switch />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setConsecModal({ open: false, rule: null })}>
                {t('ui.loyalty.cancel', { defaultValue: 'Cancel' })}
              </Button>
              <AsyncButton type="primary" htmlType="submit" loading={createConsecRuleMutation.isPending || updateConsecRuleMutation.isPending}>
                {consecModal.rule ? t('ui.loyalty.update', { defaultValue: 'Update' }) : t('ui.loyalty.create', { defaultValue: 'Create' })}
              </AsyncButton>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={streakModal.open}
        title={streakModal.rule ? t('ui.loyalty.edit_streak_rule', { defaultValue: 'Edit Streak Rule' }) : t('ui.loyalty.create_streak_rule', { defaultValue: 'Add Streak Rule' })}
        onCancel={() => setStreakModal({ open: false, rule: null })}
        footer={null}
        width={640}
      >
        <Form
          form={streakForm}
          layout="vertical"
          onFinish={(values) => {
            const payload = {
              ...values,
              min_order_amount: values.min_order_amount || null,
              program_id: selectedProgramId,
              translations: {
                name: {
                  // Mirror `en` from the canonical name field so the public
                  // /loyalty-guide page resolves English instead of falling
                  // back to another language (DEFAULT_LANGUAGE is uz).
                  en: values.name,
                  ru: values.name_ru || undefined,
                  uz: values.name_uz || undefined,
                },
              },
            };
            if (streakModal.rule) {
              updateStreakRuleMutation.mutate({ ruleId: streakModal.rule.id, values: payload });
              return;
            }
            createStreakRuleMutation.mutate(payload);
          }}
        >
          <Row gutter={16}>
            <Col span={24}>
              <Form.Item name="name" label={t('ui.loyalty.streak_name', { defaultValue: 'Name' })} rules={[{ required: true }]}>
                <Input />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="name_ru" label={t('ui.loyalty.name_ru', { defaultValue: 'Name (RU)' })}>
                <Input />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="name_uz" label={t('ui.loyalty.name_uz', { defaultValue: 'Name (UZ)' })}>
                <Input />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="required_orders" label={t('ui.loyalty.streak_required_orders', { defaultValue: 'Required Orders' })} rules={[{ required: true }]}>
                <InputNumber min={1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="window_days" label={t('ui.loyalty.streak_window_days', { defaultValue: 'Window (days)' })} rules={[{ required: true }]}>
                <InputNumber min={1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="min_order_amount" label={t('ui.loyalty.streak_min_order_amount', { defaultValue: 'Min/order (optional)' })}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="bonus_points" label={t('ui.loyalty.streak_bonus_points', { defaultValue: 'Bonus AquaCoins' })} rules={[{ required: true }]}>
                <InputNumber min={1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item name="is_active" label={t('ui.loyalty.active', { defaultValue: 'Active' })} valuePropName="checked">
            <Switch />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setStreakModal({ open: false, rule: null })}>
                {t('ui.loyalty.cancel', { defaultValue: 'Cancel' })}
              </Button>
              <AsyncButton type="primary" htmlType="submit" loading={createStreakRuleMutation.isPending || updateStreakRuleMutation.isPending}>
                {streakModal.rule ? t('ui.loyalty.update', { defaultValue: 'Update' }) : t('ui.loyalty.create', { defaultValue: 'Create' })}
              </AsyncButton>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default LoyaltyPrograms;
