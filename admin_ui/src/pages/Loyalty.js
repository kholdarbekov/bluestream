import React, { useState } from 'react';
import {
  Table,
  Card,
  Input,
  Button,
  Space,
  Tag,
  Dropdown,
  Modal,
  Form,
  Select,
  InputNumber,
  DatePicker,
  Row,
  Col,
  Statistic,
  message,
  Tabs,
  Progress,
  Badge,
  Divider,
  Switch,
  List,
  Avatar
} from 'antd';
import {
  SearchOutlined,
  GiftOutlined,
  MoreOutlined,
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  EyeOutlined,
  TrophyOutlined,
  CrownOutlined,
  StarOutlined,
  UserOutlined,
  ExportOutlined,
  PercentageOutlined,
  BgColorsOutlined
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient } from 'react-query';
import { useTranslation } from 'react-i18next';
import { formatDate, formatDateTime } from '../utils/dateUtils';
import adminService from '../services/adminService';

const { Option } = Select;
const { RangePicker } = DatePicker;
const { TextArea } = Input;

const Loyalty = () => {
  // Load loyalty namespace for ui.loyalty.* keys
  const { t } = useTranslation('loyalty');
  const [activeTab, setActiveTab] = useState('programs');
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [selectedProgram, setSelectedProgram] = useState(null);
  const [selectedCustomer, setSelectedCustomer] = useState(null);
  const [selectedTier, setSelectedTier] = useState(null);
  const [isProgramModalVisible, setIsProgramModalVisible] = useState(false);
  const [isEditProgramModalVisible, setIsEditProgramModalVisible] = useState(false);
  const [isCustomerModalVisible, setIsCustomerModalVisible] = useState(false);
  const [isTierModalVisible, setIsTierModalVisible] = useState(false);
  const [isEditTierModalVisible, setIsEditTierModalVisible] = useState(false);
  const [pagination, setPagination] = useState({ page: 1, per_page: 20 });
  const [programForm] = Form.useForm();
  const [editForm] = Form.useForm();
  const [tierForm] = Form.useForm();
  const [editTierForm] = Form.useForm();

  const queryClient = useQueryClient();

  // Fetch loyalty programs
  const { data: programsData, isLoading: programsLoading } = useQuery(
    ['loyalty-programs', pagination, searchText, statusFilter],
    () => adminService.getLoyaltyPrograms({
      page: pagination.page,
      per_page: pagination.per_page,
      search: searchText,
      status: statusFilter
    }),
    {
      keepPreviousData: true
    }
  );

  // Fetch loyalty customers
  const { data: customersData, isLoading: customersLoading } = useQuery(
    ['loyalty-customers', pagination, searchText],
    () => adminService.getLoyaltyCustomers({
      page: pagination.page,
      per_page: pagination.per_page,
      search: searchText
    }),
    {
      keepPreviousData: true,
      enabled: activeTab === 'customers'
    }
  );

  // Create program mutation
  const createProgramMutation = useMutation(
    (programData) => adminService.createLoyaltyProgram(programData),
    {
      onSuccess: () => {
        message.success(t('ui.loyalty.create_success'));
        queryClient.invalidateQueries('loyalty-programs');
        setIsProgramModalVisible(false);
        programForm.resetFields();
      },
      onError: (error) => {
        message.error(t('ui.loyalty.create_error'));
      }
    }
  );

  // Update program mutation
  const updateProgramMutation = useMutation(
    ({ programId, programData }) => adminService.updateLoyaltyProgram(programId, programData),
    {
      onSuccess: () => {
        message.success(t('ui.loyalty.update_success'));
        queryClient.invalidateQueries('loyalty-programs');
        setIsEditProgramModalVisible(false);
        editForm.resetFields();
      },
      onError: (error) => {
        message.error(t('ui.loyalty.update_error'));
      }
    }
  );

  // Fetch loyalty tiers
  const { data: tiersData, isLoading: tiersLoading } = useQuery(
    ['loyalty-tiers', activeTab],
    () => adminService.getLoyaltyTiers(),
    {
      enabled: activeTab === 'tiers'
    }
  );

  // Create tier mutation
  const createTierMutation = useMutation(
    (tierData) => adminService.createLoyaltyTier(tierData),
    {
      onSuccess: () => {
        message.success(t('ui.loyalty.tier_create_success'));
        queryClient.invalidateQueries('loyalty-tiers');
        setIsTierModalVisible(false);
        tierForm.resetFields();
      },
      onError: () => {
        message.error(t('ui.loyalty.tier_create_error'));
      }
    }
  );

  // Update tier mutation
  const updateTierMutation = useMutation(
    ({ tierId, tierData }) => adminService.updateLoyaltyTier(tierId, tierData),
    {
      onSuccess: () => {
        message.success(t('ui.loyalty.tier_update_success'));
        queryClient.invalidateQueries('loyalty-tiers');
        setIsEditTierModalVisible(false);
        editTierForm.resetFields();
      },
      onError: () => {
        message.error(t('ui.loyalty.tier_update_error'));
      }
    }
  );

  // Delete tier mutation
  const deleteTierMutation = useMutation(
    (tierId) => adminService.deleteLoyaltyTier(tierId),
    {
      onSuccess: () => {
        message.success(t('ui.loyalty.tier_delete_success'));
        queryClient.invalidateQueries('loyalty-tiers');
      },
      onError: () => {
        message.error(t('ui.loyalty.tier_delete_error'));
      }
    }
  );

  const programStatusColors = {
    active: 'green',
    inactive: 'red',
    draft: 'orange',
    expired: 'grey'
  };

  const tierColors = {
    bronze: '#CD7F32',
    silver: '#C0C0C0',
    gold: '#FFD700',
    platinum: '#E5E4E2',
    diamond: '#B9F2FF'
  };

  const programColumns = [
    {
      title: t('ui.loyalty.program_name'),
      dataIndex: 'name',
      key: 'name',
      render: (text, record) => (
        <div>
          <div style={{ fontWeight: 'bold' }}>{text}</div>
          <small style={{ color: '#666' }}>{record.description}</small>
        </div>
      )
    },
    {
      title: t('ui.loyalty.type'),
      dataIndex: 'type',
      key: 'type',
      width: 120,
      render: (type) => (
        <Tag color="blue">{type?.toUpperCase()}</Tag>
      )
    },
    {
      title: t('ui.loyalty.points_ratio'),
      dataIndex: 'points_per_dollar',
      key: 'points_per_dollar',
      width: 120,
      render: (ratio) => (
        <span>{ratio} {t('ui.loyalty.pts_per_dollar')}</span>
      )
    },
    {
      title: t('ui.loyalty.active_members'),
      dataIndex: 'active_members',
      key: 'active_members',
      width: 120,
      render: (count) => (
        <Badge count={count} style={{ backgroundColor: '#52c41a' }} />
      )
    },
    {
      title: t('ui.loyalty.status'),
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (status) => (
        <Tag color={programStatusColors[status] || 'default'}>
          {status?.toUpperCase()}
        </Tag>
      )
    },
    {
      title: t('ui.loyalty.created'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 120,
      render: (date) => formatDate(date)
    },
    {
      title: t('ui.loyalty.actions'),
      key: 'actions',
      width: 100,
      render: (_, record) => (
        <Dropdown
          menu={{
            items: [
              {
                key: 'view',
                label: t('ui.loyalty.view_details'),
                icon: <EyeOutlined />,
                onClick: () => handleViewProgram(record)
              },
              {
                key: 'edit',
                label: t('ui.loyalty.edit_program'),
                icon: <EditOutlined />,
                onClick: () => handleEditProgram(record)
              },
              {
                type: 'divider'
              },
              {
                key: 'delete',
                label: t('ui.loyalty.delete_program'),
                icon: <DeleteOutlined />,
                danger: true,
                onClick: () => handleDeleteProgram(record)
              }
            ]
          }}
          trigger={['click']}
        >
          <Button type="text" icon={<MoreOutlined />} />
        </Dropdown>
      )
    }
  ];

  const customerColumns = [
    {
      title: t('ui.loyalty.customer'),
      dataIndex: 'customer_name',
      key: 'customer_name',
      render: (text, record) => (
        <div>
          <div style={{ fontWeight: 'bold' }}>{text}</div>
          <small style={{ color: '#666' }}>{record.customer_email}</small>
        </div>
      )
    },
    {
      title: t('ui.loyalty.tier'),
      dataIndex: 'tier',
      key: 'tier',
      width: 100,
      render: (tier) => (
        <Tag color={tierColors[tier]} style={{ color: '#000' }}>
          <CrownOutlined style={{ marginRight: 4 }} />
          {tier?.toUpperCase()}
        </Tag>
      )
    },
    {
      title: t('ui.loyalty.points_balance'),
      dataIndex: 'points_balance',
      key: 'points_balance',
      width: 120,
      render: (points) => (
        <span style={{ fontWeight: 'bold', color: '#1890ff' }}>
          {points?.toLocaleString()} {t('ui.loyalty.pts')}
        </span>
      )
    },
    {
      title: t('ui.loyalty.total_earned'),
      dataIndex: 'total_points_earned',
      key: 'total_points_earned',
      width: 120,
      render: (points) => (
        <span>{points?.toLocaleString()} {t('ui.loyalty.pts')}</span>
      )
    },
    {
      title: t('ui.loyalty.last_activity'),
      dataIndex: 'last_activity',
      key: 'last_activity',
      width: 120,
      render: (date) => formatDate(date)
    },
    {
      title: t('ui.loyalty.actions'),
      key: 'actions',
      width: 100,
      render: (_, record) => (
        <Button
          type="text"
          icon={<EyeOutlined />}
          onClick={() => handleViewCustomer(record)}
        />
      )
    }
  ];

  const tierColumns = [
    {
      title: t('ui.loyalty.tier_name'),
      dataIndex: 'name',
      key: 'name',
      render: (text, record) => (
        <div>
          <Tag color={record.color || '#CD7F32'}>
            <i className={`far ${record.icon || 'fa-medal'}`} style={{ marginRight: 5 }} />
            {text}
          </Tag>
        </div>
      )
    },
    {
      title: t('ui.loyalty.points_range'),
      key: 'points',
      render: (_, record) => (
        <span>
          {record.min_points.toLocaleString()}
          {record.max_points ? ` - ${record.max_points.toLocaleString()}` : '+'}
          {' '}{t('ui.loyalty.pts')}
        </span>
      )
    },
    {
      title: t('ui.loyalty.multiplier'),
      dataIndex: 'points_multiplier',
      key: 'multiplier',
      render: (multiplier) => (
        <Tag color="cyan">{multiplier}x</Tag>
      )
    },
    {
      title: t('ui.loyalty.discount'),
      dataIndex: 'discount_percentage',
      key: 'discount',
      render: (discount) => (
        <span>{discount}%</span>
      )
    },
    {
      title: t('ui.loyalty.display_order'),
      dataIndex: 'display_order',
      key: 'display_order',
      sorter: (a, b) => a.display_order - b.display_order,
    },
    {
      title: t('ui.loyalty.status'),
      dataIndex: 'is_active',
      key: 'is_active',
      render: (isActive) => (
        <Tag color={isActive ? 'green' : 'red'}>
          {isActive ? t('ui.loyalty.active') : t('ui.loyalty.inactive')}
        </Tag>
      )
    },
    {
      title: t('ui.loyalty.actions'),
      key: 'actions',
      render: (_, record) => (
        <Space>
          <Button
            type="text"
            icon={<EditOutlined />}
            onClick={() => handleEditTier(record)}
          />
          <Button
            type="text"
            danger
            icon={<DeleteOutlined />}
            onClick={() => handleDeleteTier(record)}
          />
        </Space>
      )
    }
  ];

  const handleCreateTier = (values) => {
    createTierMutation.mutate(values);
  };

  const handleUpdateTier = (values) => {
    updateTierMutation.mutate({
      tierId: selectedTier.id,
      tierData: values
    });
  };

  const handleEditTier = (tier) => {
    setSelectedTier(tier);
    editTierForm.setFieldsValue(tier);
    setIsEditTierModalVisible(true);
  };

  const handleDeleteTier = (tier) => {
    Modal.confirm({
      title: t('ui.loyalty.delete_tier_confirm_title'),
      content: t('ui.loyalty.delete_tier_confirm_message', { name: tier.name }),
      okText: t('ui.common.yes'),
      okType: 'danger',
      cancelText: t('ui.common.no'),
      onOk: () => {
        deleteTierMutation.mutate(tier.id);
      }
    });
  };


  const handleViewProgram = (program) => {
    setSelectedProgram(program);
    // Implementation for viewing program details
  };

  const handleEditProgram = (program) => {
    setSelectedProgram(program);
    editForm.setFieldsValue({
      name: program.name,
      description: program.description,
      type: program.type,
      points_per_dollar: program.points_per_dollar,
      status: program.status,
      min_purchase_amount: program.min_purchase_amount,
      expiry_months: program.expiry_months
    });
    setIsEditProgramModalVisible(true);
  };

  const handleDeleteProgram = (program) => {
    Modal.confirm({
      title: t('ui.loyalty.delete_confirm_title'),
      content: t('ui.loyalty.delete_confirm_message', { name: program.name }),
      onOk: () => {
        message.success(t('ui.loyalty.delete_success'));
        queryClient.invalidateQueries('loyalty-programs');
      }
    });
  };

  const handleViewCustomer = (customer) => {
    setSelectedCustomer(customer);
    setIsCustomerModalVisible(true);
  };

  const handleCreateProgram = (values) => {
    createProgramMutation.mutate(values);
  };

  const handleUpdateProgram = (values) => {
    updateProgramMutation.mutate({
      programId: selectedProgram.id,
      programData: values
    });
  };

  const handleTableChange = (paginationInfo) => {
    setPagination({
      page: paginationInfo.current,
      per_page: paginationInfo.pageSize
    });
  };

  const handleSearch = (value) => {
    setSearchText(value);
    setPagination({ ...pagination, page: 1 });
  };

  // Calculate summary statistics
  const programs = programsData?.programs || [];
  const customers = customersData?.customers || [];
  const totalPrograms = programsData?.pagination?.total || 0;
  const activePrograms = programs.filter(p => p.status === 'active').length;
  const totalLoyaltyMembers = customersData?.pagination?.total || 0;
  const totalPointsDistributed = customers.reduce((sum, customer) => sum + (customer.total_points_earned || 0), 0);

  const tabItems = [
    {
      key: 'programs',
      label: t('ui.loyalty.tab_programs'),
      children: (
        <div>
          {/* Summary Cards for Programs */}
          <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title={t('ui.loyalty.total_programs')}
                  value={totalPrograms}
                  prefix={<GiftOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title={t('ui.loyalty.active_programs')}
                  value={activePrograms}
                  valueStyle={{ color: '#52c41a' }}
                  prefix={<TrophyOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title={t('ui.loyalty.total_members')}
                  value={totalLoyaltyMembers}
                  prefix={<UserOutlined />}
                />
              </Card>
            </Col>
          </Row>

          <Card>
            {/* Filter Controls */}
            <div className="table-actions">
              <Space wrap>
                <Input.Search
                  placeholder={t('ui.loyalty.search_programs')}
                  allowClear
                  onSearch={handleSearch}
                  style={{ width: 250 }}
                />
                <Select
                  placeholder={t('ui.loyalty.filter_by_status')}
                  allowClear
                  onChange={setStatusFilter}
                  style={{ width: 150 }}
                >
                  <Option value="active">{t('ui.loyalty.status_active')}</Option>
                  <Option value="inactive">{t('ui.loyalty.status_inactive')}</Option>
                  <Option value="draft">{t('ui.loyalty.status_draft')}</Option>
                  <Option value="expired">{t('ui.loyalty.status_expired')}</Option>
                </Select>
              </Space>

              <Space>
                <Button
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={() => setIsProgramModalVisible(true)}
                >
                  {t('ui.loyalty.create_program')}
                </Button>
                <Button icon={<ExportOutlined />}>
                  {t('ui.loyalty.export_data')}
                </Button>
              </Space>
            </div>

            <Table
              columns={programColumns}
              dataSource={programs}
              loading={programsLoading}
              rowKey="id"
              pagination={{
                current: pagination.page,
                pageSize: pagination.per_page,
                total: totalPrograms,
                showSizeChanger: true,
                showQuickJumper: true,
                showTotal: (total, range) =>
                  t('ui.loyalty.pagination_programs', { from: range[0], to: range[1], total })
              }}
              onChange={handleTableChange}
              className="admin-table"
            />
          </Card>
        </div>
      )
    },
    {
      key: 'customers',
      label: t('ui.loyalty.tab_members'),
      children: (
        <div>
          {/* Summary Cards for Customers */}
          <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title={t('ui.loyalty.total_members')}
                  value={totalLoyaltyMembers}
                  prefix={<UserOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title={t('ui.loyalty.points_distributed')}
                  value={totalPointsDistributed}
                  prefix={<StarOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title={t('ui.loyalty.avg_points_per_member')}
                  value={totalLoyaltyMembers > 0 ? Math.round(totalPointsDistributed / totalLoyaltyMembers) : 0}
                  prefix={<TrophyOutlined />}
                />
              </Card>
            </Col>
          </Row>

          <Card>
            <div className="table-actions">
              <Space wrap>
                <Input.Search
                  placeholder={t('ui.loyalty.search_members')}
                  allowClear
                  onSearch={handleSearch}
                  style={{ width: 250 }}
                />
              </Space>

              <Space>
                <Button icon={<ExportOutlined />}>
                  {t('ui.loyalty.export_members')}
                </Button>
              </Space>
            </div>

            <Table
              columns={customerColumns}
              dataSource={customers}
              loading={customersLoading}
              rowKey="id"
              pagination={{
                current: pagination.page,
                pageSize: pagination.per_page,
                total: totalLoyaltyMembers,
                showSizeChanger: true,
                showQuickJumper: true,
                showTotal: (total, range) =>
                  t('ui.loyalty.pagination_members', { from: range[0], to: range[1], total })
              }}
              onChange={handleTableChange}
              className="admin-table"
            />
          </Card>
        </div>
      )
    },
    {
      key: 'tiers',
      label: t('ui.loyalty.tab_tiers'),
      children: (
        <div>
          {/* Summary Cards for Tiers */}
          <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title={t('ui.loyalty.active_tiers')}
                  value={tiersData?.data?.tier_count || 0}
                  prefix={<CrownOutlined />}
                  valueStyle={{ color: '#722ed1' }}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title={t('ui.loyalty.max_multiplier')}
                  value={Math.max(...(tiersData?.data?.tiers || []).map(t => t.points_multiplier), 1)}
                  precision={1}
                  suffix="x"
                  prefix={<PercentageOutlined />}
                  valueStyle={{ color: '#1890ff' }}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title={t('ui.loyalty.tier_programs')}
                  value={1} // Currently supporting single program
                  prefix={<TrophyOutlined />}
                />
              </Card>
            </Col>
          </Row>

          <Card>
            <div className="table-actions">
              <Space>
                <Button
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={() => {
                    tierForm.resetFields();
                    tierForm.setFieldsValue({
                      points_multiplier: 1.0,
                      discount_percentage: 0,
                      display_order: (tiersData?.data?.tiers?.length || 0),
                      color: '#CD7F32',
                      is_active: true
                    });
                    setIsTierModalVisible(true);
                  }}
                >
                  {t('ui.loyalty.create_tier')}
                </Button>
              </Space>
            </div>

            <Table
              columns={tierColumns}
              dataSource={tiersData?.data?.tiers || []}
              loading={tiersLoading}
              rowKey="id"
              pagination={false} // Tiers list is usually short
              className="admin-table"
            />
          </Card>
        </div>
      )
    }
  ];

  return (
    <div>
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={tabItems}
      />

      {/* Create Program Modal */}
      <Modal
        title={t('ui.loyalty.modal_create_title')}
        open={isProgramModalVisible}
        onCancel={() => setIsProgramModalVisible(false)}
        footer={null}
        width={600}
      >
        <Form
          form={programForm}
          layout="vertical"
          onFinish={handleCreateProgram}
        >
          <Form.Item
            name="name"
            label={t('ui.loyalty.form_program_name')}
            rules={[{ required: true, message: t('ui.loyalty.form_program_name_required') }]}
          >
            <Input placeholder={t('ui.loyalty.form_program_name_placeholder')} />
          </Form.Item>

          <Form.Item
            name="description"
            label={t('ui.loyalty.form_description')}
            rules={[{ required: true, message: t('ui.loyalty.form_description_required') }]}
          >
            <TextArea rows={3} placeholder={t('ui.loyalty.form_description_placeholder')} />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="type"
                label={t('ui.loyalty.form_program_type')}
                rules={[{ required: true, message: t('ui.loyalty.form_program_type_required') }]}
              >
                <Select placeholder={t('ui.loyalty.form_program_type_placeholder')}>
                  <Option value="points">{t('ui.loyalty.type_points')}</Option>
                  <Option value="tier">{t('ui.loyalty.type_tier')}</Option>
                  <Option value="cashback">{t('ui.loyalty.type_cashback')}</Option>
                  <Option value="discount">{t('ui.loyalty.type_discount')}</Option>
                </Select>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="status"
                label={t('ui.loyalty.form_status')}
                rules={[{ required: true, message: t('ui.loyalty.form_status_required') }]}
              >
                <Select placeholder={t('ui.loyalty.form_status_placeholder')}>
                  <Option value="active">{t('ui.loyalty.status_active')}</Option>
                  <Option value="inactive">{t('ui.loyalty.status_inactive')}</Option>
                  <Option value="draft">{t('ui.loyalty.status_draft')}</Option>
                </Select>
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="points_per_dollar"
                label={t('ui.loyalty.form_points_per_dollar')}
                rules={[{ required: true, message: t('ui.loyalty.form_points_per_dollar_required') }]}
              >
                <InputNumber
                  placeholder="1"
                  style={{ width: '100%' }}
                  min={0.1}
                  step={0.1}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="min_purchase_amount"
                label={t('ui.loyalty.form_min_purchase')}
              >
                <InputNumber
                  placeholder="0.00"
                  prefix="$"
                  style={{ width: '100%' }}
                  min={0}
                  precision={2}
                />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item
            name="expiry_months"
            label={t('ui.loyalty.form_expiry_months')}
          >
            <InputNumber
              placeholder="12"
              style={{ width: '100%' }}
              min={1}
              max={120}
            />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setIsProgramModalVisible(false)}>
                {t('ui.loyalty.cancel')}
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={createProgramMutation.isLoading}
              >
                {t('ui.loyalty.create_program')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* Edit Program Modal */}
      <Modal
        title={t('ui.loyalty.modal_edit_title', { name: selectedProgram?.name })}
        open={isEditProgramModalVisible}
        onCancel={() => setIsEditProgramModalVisible(false)}
        footer={null}
        width={600}
      >
        <Form
          form={editForm}
          layout="vertical"
          onFinish={handleUpdateProgram}
        >
          <Form.Item
            name="name"
            label={t('ui.loyalty.form_program_name')}
            rules={[{ required: true, message: t('ui.loyalty.form_program_name_required') }]}
          >
            <Input placeholder={t('ui.loyalty.form_program_name_placeholder')} />
          </Form.Item>

          <Form.Item
            name="description"
            label={t('ui.loyalty.form_description')}
            rules={[{ required: true, message: t('ui.loyalty.form_description_required') }]}
          >
            <TextArea rows={3} placeholder={t('ui.loyalty.form_description_placeholder')} />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="type"
                label={t('ui.loyalty.form_program_type')}
                rules={[{ required: true, message: t('ui.loyalty.form_program_type_required') }]}
              >
                <Select placeholder={t('ui.loyalty.form_program_type_placeholder')}>
                  <Option value="points">{t('ui.loyalty.type_points')}</Option>
                  <Option value="tier">{t('ui.loyalty.type_tier')}</Option>
                  <Option value="cashback">{t('ui.loyalty.type_cashback')}</Option>
                  <Option value="discount">{t('ui.loyalty.type_discount')}</Option>
                </Select>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="status"
                label={t('ui.loyalty.form_status')}
                rules={[{ required: true, message: t('ui.loyalty.form_status_required') }]}
              >
                <Select placeholder={t('ui.loyalty.form_status_placeholder')}>
                  <Option value="active">{t('ui.loyalty.status_active')}</Option>
                  <Option value="inactive">{t('ui.loyalty.status_inactive')}</Option>
                  <Option value="draft">{t('ui.loyalty.status_draft')}</Option>
                </Select>
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="points_per_dollar"
                label={t('ui.loyalty.form_points_per_dollar')}
                rules={[{ required: true, message: t('ui.loyalty.form_points_per_dollar_required') }]}
              >
                <InputNumber
                  placeholder="1"
                  style={{ width: '100%' }}
                  min={0.1}
                  step={0.1}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="min_purchase_amount"
                label={t('ui.loyalty.form_min_purchase')}
              >
                <InputNumber
                  placeholder="0.00"
                  prefix="$"
                  style={{ width: '100%' }}
                  min={0}
                  precision={2}
                />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item
            name="expiry_months"
            label={t('ui.loyalty.form_expiry_months')}
          >
            <InputNumber
              placeholder="12"
              style={{ width: '100%' }}
              min={1}
              max={120}
            />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setIsEditProgramModalVisible(false)}>
                {t('ui.loyalty.cancel')}
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={updateProgramMutation.isLoading}
              >
                {t('ui.loyalty.update_program')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* Customer Details Modal */}
      <Modal
        title={t('ui.loyalty.modal_member_details', { name: selectedCustomer?.customer_name })}
        open={isCustomerModalVisible}
        onCancel={() => setIsCustomerModalVisible(false)}
        footer={null}
        width={600}
      >
        {selectedCustomer && (
          <div>
            <Row gutter={16}>
              <Col span={12}>
                <Card size="small">
                  <Statistic
                    title={t('ui.loyalty.current_points')}
                    value={selectedCustomer.points_balance}
                    suffix={t('ui.loyalty.pts')}
                    valueStyle={{ color: '#1890ff' }}
                  />
                </Card>
              </Col>
              <Col span={12}>
                <Card size="small">
                  <Statistic
                    title={t('ui.loyalty.total_earned')}
                    value={selectedCustomer.total_points_earned}
                    suffix={t('ui.loyalty.pts')}
                    valueStyle={{ color: '#52c41a' }}
                  />
                </Card>
              </Col>
            </Row>

            <Divider>{t('ui.loyalty.tier_information')}</Divider>
            <div style={{ textAlign: 'center', marginBottom: 16 }}>
              <Tag color={tierColors[selectedCustomer.tier]} size="large" style={{ color: '#000' }}>
                <CrownOutlined style={{ marginRight: 4 }} />
                {selectedCustomer.tier?.toUpperCase()} {t('ui.loyalty.member')}
              </Tag>
            </div>

            <Divider>{t('ui.loyalty.recent_activity')}</Divider>
            <List
              size="small"
              dataSource={selectedCustomer.recent_activities || []}
              renderItem={item => (
                <List.Item>
                  <List.Item.Meta
                    avatar={<Avatar icon={<StarOutlined />} />}
                    title={item.activity}
                    description={formatDateTime(item.date)}
                  />
                  <div>{item.points} {t('ui.loyalty.pts')}</div>
                </List.Item>
              )}
            />

            <div style={{ marginTop: 16, textAlign: 'right' }}>
              <Button type="primary" onClick={() => setIsCustomerModalVisible(false)}>
                {t('ui.loyalty.close')}
              </Button>
            </div>
          </div>
        )}
      </Modal>

      {/* Create Tier Modal */}
      <Modal
        title={t('ui.loyalty.create_tier')}
        open={isTierModalVisible}
        onCancel={() => setIsTierModalVisible(false)}
        footer={null}
        width={600}
      >
        <Form
          form={tierForm}
          layout="vertical"
          onFinish={handleCreateTier}
        >
          <Form.Item
            name="name"
            label={t('ui.loyalty.tier_name')}
            rules={[{ required: true, message: t('ui.loyalty.tier_name_required') }]}
          >
            <Input placeholder="e.g. Bronze, Silver" />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="min_points"
                label={t('ui.loyalty.min_points')}
                rules={[{ required: true, message: t('ui.loyalty.min_points_required') }]}
              >
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="max_points"
                label={t('ui.loyalty.max_points')}
                tooltip={t('ui.loyalty.max_points_tooltip')}
              >
                <InputNumber min={0} style={{ width: '100%' }} placeholder="Leave empty for highest tier" />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="points_multiplier"
                label={t('ui.loyalty.multiplier')}
                rules={[{ required: true, message: t('ui.loyalty.multiplier_required') }]}
              >
                <InputNumber min={1.0} step={0.1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="discount_percentage"
                label={t('ui.loyalty.discount_percent')}
              >
                <InputNumber min={0} max={100} style={{ width: '100%' }} formatter={value => `${value}%`} parser={value => value.replace('%', '')} />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="color"
                label={t('ui.loyalty.tier_color')}
              >
                <Input prefix={<BgColorsOutlined />} placeholder="#CD7F32" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="icon"
                label={t('ui.loyalty.tier_icon')}
              >
                <Input prefix={<StarOutlined />} placeholder="fa-medal" />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="display_order"
                label={t('ui.loyalty.display_order')}
              >
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="is_active"
                label={t('ui.loyalty.status')}
                valuePropName="checked"
              >
                <Switch
                  checkedChildren={t('ui.loyalty.active')}
                  unCheckedChildren={t('ui.loyalty.inactive')}
                />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setIsTierModalVisible(false)}>
                {t('ui.loyalty.cancel')}
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={createTierMutation.isLoading}
              >
                {t('ui.loyalty.create')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* Edit Tier Modal */}
      <Modal
        title={t('ui.loyalty.edit_tier')}
        open={isEditTierModalVisible}
        onCancel={() => setIsEditTierModalVisible(false)}
        footer={null}
        width={600}
      >
        <Form
          form={editTierForm}
          layout="vertical"
          onFinish={handleUpdateTier}
        >
          <Form.Item
            name="name"
            label={t('ui.loyalty.tier_name')}
            rules={[{ required: true, message: t('ui.loyalty.tier_name_required') }]}
          >
            <Input placeholder="e.g. Bronze, Silver" />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="min_points"
                label={t('ui.loyalty.min_points')}
                rules={[{ required: true, message: t('ui.loyalty.min_points_required') }]}
              >
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="max_points"
                label={t('ui.loyalty.max_points')}
                tooltip={t('ui.loyalty.max_points_tooltip')}
              >
                <InputNumber min={0} style={{ width: '100%' }} placeholder="Leave empty for highest tier" />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="points_multiplier"
                label={t('ui.loyalty.multiplier')}
                rules={[{ required: true, message: t('ui.loyalty.multiplier_required') }]}
              >
                <InputNumber min={1.0} step={0.1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="discount_percentage"
                label={t('ui.loyalty.discount_percent')}
              >
                <InputNumber min={0} max={100} style={{ width: '100%' }} formatter={value => `${value}%`} parser={value => value.replace('%', '')} />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="color"
                label={t('ui.loyalty.tier_color')}
              >
                <Input prefix={<BgColorsOutlined />} placeholder="#CD7F32" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="icon"
                label={t('ui.loyalty.tier_icon')}
              >
                <Input prefix={<StarOutlined />} placeholder="fa-medal" />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="display_order"
                label={t('ui.loyalty.display_order')}
              >
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="is_active"
                label={t('ui.loyalty.status')}
                valuePropName="checked"
              >
                <Switch
                  checkedChildren={t('ui.loyalty.active')}
                  unCheckedChildren={t('ui.loyalty.inactive')}
                />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setIsEditTierModalVisible(false)}>
                {t('ui.loyalty.cancel')}
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={updateTierMutation.isLoading}
              >
                {t('ui.loyalty.update')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default Loyalty;