import React, { useEffect, useMemo, useState } from 'react';
import {
  Button,
  Card,
  Col,
  Empty,
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
import { useMutation, useQuery, useQueryClient } from 'react-query';
import { useTranslation } from 'react-i18next';
import adminService from '../services/adminService';
import exportUtils from '../utils/exportUtils';
import { formatDate } from '../utils/dateUtils';

const { TextArea } = Input;

const LoyaltyPrograms = () => {
  const { t } = useTranslation('loyalty');
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState('programs');
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState();
  const [selectedProgramId, setSelectedProgramId] = useState();
  const [pagination, setPagination] = useState({ page: 1, per_page: 20 });
  const [programModal, setProgramModal] = useState({ open: false, program: null });
  const [tierModal, setTierModal] = useState({ open: false, tier: null });
  const [programForm] = Form.useForm();
  const [tierForm] = Form.useForm();

  const programsQuery = useQuery(
    ['loyalty-programs', pagination, searchText, statusFilter],
    () => adminService.getLoyaltyPrograms({
      page: pagination.page,
      per_page: pagination.per_page,
      search: searchText,
      status: statusFilter,
    }),
    { keepPreviousData: true }
  );

  const programs = programsQuery.data?.items || [];
  const totalPrograms = programsQuery.data?.total || 0;

  useEffect(() => {
    if (!selectedProgramId && programs.length > 0) {
      const defaultProgram = programs.find((program) => program.is_default);
      setSelectedProgramId((defaultProgram || programs[0]).id);
    }
  }, [programs, selectedProgramId]);

  const tiersQuery = useQuery(
    ['loyalty-tiers', selectedProgramId],
    () => adminService.getLoyaltyTiers({ program_id: selectedProgramId }),
    {
      enabled: Boolean(selectedProgramId),
      keepPreviousData: true,
    }
  );

  const tiers = tiersQuery.data?.items || [];

  const invalidateLoyaltyQueries = () => {
    queryClient.invalidateQueries(['loyalty-programs']);
    queryClient.invalidateQueries(['loyalty-tiers']);
    queryClient.invalidateQueries(['loyalty-program-options']);
  };

  const createProgramMutation = useMutation(
    (values) => adminService.createLoyaltyProgram(values),
    {
      onSuccess: () => {
        message.success(t('ui.loyalty.create_success', { defaultValue: 'Program created successfully' }));
        setProgramModal({ open: false, program: null });
        programForm.resetFields();
        invalidateLoyaltyQueries();
      }
    }
  );

  const updateProgramMutation = useMutation(
    ({ programId, values }) => adminService.updateLoyaltyProgram(programId, values),
    {
      onSuccess: () => {
        message.success(t('ui.loyalty.update_success', { defaultValue: 'Program updated successfully' }));
        setProgramModal({ open: false, program: null });
        programForm.resetFields();
        invalidateLoyaltyQueries();
      }
    }
  );

  const deleteProgramMutation = useMutation(
    (programId) => adminService.deleteLoyaltyProgram(programId),
    {
      onSuccess: () => {
        message.success(t('ui.loyalty.delete_success', { defaultValue: 'Program updated successfully' }));
        invalidateLoyaltyQueries();
      }
    }
  );

  const createTierMutation = useMutation(
    (values) => adminService.createLoyaltyTier(values),
    {
      onSuccess: () => {
        message.success(t('ui.loyalty.tier_create_success', { defaultValue: 'Tier created successfully' }));
        setTierModal({ open: false, tier: null });
        tierForm.resetFields();
        invalidateLoyaltyQueries();
      }
    }
  );

  const updateTierMutation = useMutation(
    ({ tierId, values }) => adminService.updateLoyaltyTier(tierId, values),
    {
      onSuccess: () => {
        message.success(t('ui.loyalty.tier_update_success', { defaultValue: 'Tier updated successfully' }));
        setTierModal({ open: false, tier: null });
        tierForm.resetFields();
        invalidateLoyaltyQueries();
      }
    }
  );

  const deleteTierMutation = useMutation(
    (tierId) => adminService.deleteLoyaltyTier(tierId),
    {
      onSuccess: () => {
        message.success(t('ui.loyalty.tier_delete_success', { defaultValue: 'Tier removed successfully' }));
        invalidateLoyaltyQueries();
      }
    }
  );

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
      title: t('ui.loyalty.uzs_per_point', { defaultValue: 'UZS per Point' }),
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
      title: t('ui.loyalty.points_range', { defaultValue: 'Points Range' }),
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
              tierForm.setFieldsValue(record);
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
                      emptyText: <Empty description={t('ui.loyalty.no_programs', { defaultValue: 'No loyalty programs found' })} />
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
                      emptyText: <Empty description={t('ui.loyalty.no_tiers', { defaultValue: 'No tiers configured' })} />
                    }}
                    pagination={false}
                  />
                </>
              )
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
            if (programModal.program) {
              updateProgramMutation.mutate({ programId: programModal.program.id, values });
              return;
            }
            createProgramMutation.mutate(values);
          }}
        >
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="name" label={t('ui.loyalty.program_name', { defaultValue: 'Program Name' })} rules={[{ required: true }]}>
                <Input />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="uzs_per_point" label={t('ui.loyalty.uzs_per_point', { defaultValue: 'UZS per Point' })} rules={[{ required: true }]}>
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
              <Form.Item name="points_expiry_days" label={t('ui.loyalty.points_expiry_days', { defaultValue: 'Points Expiry Days' })}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="min_redemption_points" label={t('ui.loyalty.min_redemption_points', { defaultValue: 'Minimum Redemption Points' })}>
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

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setProgramModal({ open: false, program: null })}>
                {t('ui.loyalty.cancel', { defaultValue: 'Cancel' })}
              </Button>
              <Button type="primary" htmlType="submit" loading={createProgramMutation.isLoading || updateProgramMutation.isLoading}>
                {programModal.program ? t('ui.loyalty.update_program', { defaultValue: 'Update Program' }) : t('ui.loyalty.create_program', { defaultValue: 'Create Program' })}
              </Button>
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
              <Form.Item name="min_points" label={t('ui.loyalty.min_points', { defaultValue: 'Minimum Points' })} rules={[{ required: true }]}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="max_points" label={t('ui.loyalty.max_points', { defaultValue: 'Maximum Points' })}>
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
              <Button type="primary" htmlType="submit" loading={createTierMutation.isLoading || updateTierMutation.isLoading}>
                {tierModal.tier ? t('ui.loyalty.update', { defaultValue: 'Update' }) : t('ui.loyalty.create', { defaultValue: 'Create' })}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default LoyaltyPrograms;
