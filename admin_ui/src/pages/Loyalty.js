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
  PercentageOutlined
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient } from 'react-query';
import moment from 'moment';
import adminService from '../services/adminService';

const { Option } = Select;
const { RangePicker } = DatePicker;
const { TextArea } = Input;

const Loyalty = () => {
  const [activeTab, setActiveTab] = useState('programs');
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [selectedProgram, setSelectedProgram] = useState(null);
  const [selectedCustomer, setSelectedCustomer] = useState(null);
  const [isProgramModalVisible, setIsProgramModalVisible] = useState(false);
  const [isEditProgramModalVisible, setIsEditProgramModalVisible] = useState(false);
  const [isCustomerModalVisible, setIsCustomerModalVisible] = useState(false);
  const [pagination, setPagination] = useState({ page: 1, per_page: 20 });
  const [programForm] = Form.useForm();
  const [editForm] = Form.useForm();

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
        message.success('Loyalty program created successfully');
        queryClient.invalidateQueries('loyalty-programs');
        setIsProgramModalVisible(false);
        programForm.resetFields();
      },
      onError: (error) => {
        message.error('Failed to create loyalty program');
      }
    }
  );

  // Update program mutation
  const updateProgramMutation = useMutation(
    ({ programId, programData }) => adminService.updateLoyaltyProgram(programId, programData),
    {
      onSuccess: () => {
        message.success('Loyalty program updated successfully');
        queryClient.invalidateQueries('loyalty-programs');
        setIsEditProgramModalVisible(false);
        editForm.resetFields();
      },
      onError: (error) => {
        message.error('Failed to update loyalty program');
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
      title: 'Program Name',
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
      title: 'Type',
      dataIndex: 'type',
      key: 'type',
      width: 120,
      render: (type) => (
        <Tag color="blue">{type?.toUpperCase()}</Tag>
      )
    },
    {
      title: 'Points Ratio',
      dataIndex: 'points_per_dollar',
      key: 'points_per_dollar',
      width: 120,
      render: (ratio) => (
        <span>{ratio} pts/$1</span>
      )
    },
    {
      title: 'Active Members',
      dataIndex: 'active_members',
      key: 'active_members',
      width: 120,
      render: (count) => (
        <Badge count={count} style={{ backgroundColor: '#52c41a' }} />
      )
    },
    {
      title: 'Status',
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
      title: 'Created',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 120,
      render: (date) => moment(date).format('MMM DD, YYYY')
    },
    {
      title: 'Actions',
      key: 'actions',
      width: 100,
      render: (_, record) => (
        <Dropdown
          menu={{
            items: [
              {
                key: 'view',
                label: 'View Details',
                icon: <EyeOutlined />,
                onClick: () => handleViewProgram(record)
              },
              {
                key: 'edit',
                label: 'Edit Program',
                icon: <EditOutlined />,
                onClick: () => handleEditProgram(record)
              },
              {
                type: 'divider'
              },
              {
                key: 'delete',
                label: 'Delete Program',
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
      title: 'Customer',
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
      title: 'Tier',
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
      title: 'Points Balance',
      dataIndex: 'points_balance',
      key: 'points_balance',
      width: 120,
      render: (points) => (
        <span style={{ fontWeight: 'bold', color: '#1890ff' }}>
          {points?.toLocaleString()} pts
        </span>
      )
    },
    {
      title: 'Total Earned',
      dataIndex: 'total_points_earned',
      key: 'total_points_earned',
      width: 120,
      render: (points) => (
        <span>{points?.toLocaleString()} pts</span>
      )
    },
    {
      title: 'Last Activity',
      dataIndex: 'last_activity',
      key: 'last_activity',
      width: 120,
      render: (date) => moment(date).format('MMM DD, YYYY')
    },
    {
      title: 'Actions',
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
      title: 'Delete Program?',
      content: `Are you sure you want to delete "${program.name}"?`,
      onOk: () => {
        message.success('Program deleted successfully');
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
      label: 'Loyalty Programs',
      children: (
        <div>
          {/* Summary Cards for Programs */}
          <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title="Total Programs"
                  value={totalPrograms}
                  prefix={<GiftOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title="Active Programs"
                  value={activePrograms}
                  valueStyle={{ color: '#52c41a' }}
                  prefix={<TrophyOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title="Total Members"
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
                  placeholder="Search programs..."
                  allowClear
                  onSearch={handleSearch}
                  style={{ width: 250 }}
                />
                <Select
                  placeholder="Filter by status"
                  allowClear
                  onChange={setStatusFilter}
                  style={{ width: 150 }}
                >
                  <Option value="active">Active</Option>
                  <Option value="inactive">Inactive</Option>
                  <Option value="draft">Draft</Option>
                  <Option value="expired">Expired</Option>
                </Select>
              </Space>

              <Space>
                <Button
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={() => setIsProgramModalVisible(true)}
                >
                  Create Program
                </Button>
                <Button icon={<ExportOutlined />}>
                  Export Data
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
                  `${range[0]}-${range[1]} of ${total} programs`
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
      label: 'Loyalty Members',
      children: (
        <div>
          {/* Summary Cards for Customers */}
          <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title="Total Members"
                  value={totalLoyaltyMembers}
                  prefix={<UserOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title="Points Distributed"
                  value={totalPointsDistributed}
                  prefix={<StarOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title="Avg Points per Member"
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
                  placeholder="Search members..."
                  allowClear
                  onSearch={handleSearch}
                  style={{ width: 250 }}
                />
              </Space>

              <Space>
                <Button icon={<ExportOutlined />}>
                  Export Members
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
                  `${range[0]}-${range[1]} of ${total} members`
              }}
              onChange={handleTableChange}
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
        title="Create Loyalty Program"
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
            label="Program Name"
            rules={[{ required: true, message: 'Please enter program name' }]}
          >
            <Input placeholder="Enter program name" />
          </Form.Item>

          <Form.Item
            name="description"
            label="Description"
            rules={[{ required: true, message: 'Please enter description' }]}
          >
            <TextArea rows={3} placeholder="Enter program description" />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="type"
                label="Program Type"
                rules={[{ required: true, message: 'Please select type' }]}
              >
                <Select placeholder="Select type">
                  <Option value="points">Points Based</Option>
                  <Option value="tier">Tier Based</Option>
                  <Option value="cashback">Cashback</Option>
                  <Option value="discount">Discount</Option>
                </Select>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="status"
                label="Status"
                rules={[{ required: true, message: 'Please select status' }]}
              >
                <Select placeholder="Select status">
                  <Option value="active">Active</Option>
                  <Option value="inactive">Inactive</Option>
                  <Option value="draft">Draft</Option>
                </Select>
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="points_per_dollar"
                label="Points per Dollar"
                rules={[{ required: true, message: 'Please enter points ratio' }]}
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
                label="Minimum Purchase"
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
            label="Points Expiry (Months)"
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
                Cancel
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={createProgramMutation.isLoading}
              >
                Create Program
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* Edit Program Modal */}
      <Modal
        title={`Edit Program - ${selectedProgram?.name}`}
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
            label="Program Name"
            rules={[{ required: true, message: 'Please enter program name' }]}
          >
            <Input placeholder="Enter program name" />
          </Form.Item>

          <Form.Item
            name="description"
            label="Description"
            rules={[{ required: true, message: 'Please enter description' }]}
          >
            <TextArea rows={3} placeholder="Enter program description" />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="type"
                label="Program Type"
                rules={[{ required: true, message: 'Please select type' }]}
              >
                <Select placeholder="Select type">
                  <Option value="points">Points Based</Option>
                  <Option value="tier">Tier Based</Option>
                  <Option value="cashback">Cashback</Option>
                  <Option value="discount">Discount</Option>
                </Select>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="status"
                label="Status"
                rules={[{ required: true, message: 'Please select status' }]}
              >
                <Select placeholder="Select status">
                  <Option value="active">Active</Option>
                  <Option value="inactive">Inactive</Option>
                  <Option value="draft">Draft</Option>
                </Select>
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="points_per_dollar"
                label="Points per Dollar"
                rules={[{ required: true, message: 'Please enter points ratio' }]}
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
                label="Minimum Purchase"
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
            label="Points Expiry (Months)"
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
                Cancel
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={updateProgramMutation.isLoading}
              >
                Update Program
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* Customer Details Modal */}
      <Modal
        title={`Member Details - ${selectedCustomer?.customer_name}`}
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
                    title="Current Points"
                    value={selectedCustomer.points_balance}
                    suffix="pts"
                    valueStyle={{ color: '#1890ff' }}
                  />
                </Card>
              </Col>
              <Col span={12}>
                <Card size="small">
                  <Statistic
                    title="Total Earned"
                    value={selectedCustomer.total_points_earned}
                    suffix="pts"
                    valueStyle={{ color: '#52c41a' }}
                  />
                </Card>
              </Col>
            </Row>

            <Divider>Tier Information</Divider>
            <div style={{ textAlign: 'center', marginBottom: 16 }}>
              <Tag color={tierColors[selectedCustomer.tier]} size="large" style={{ color: '#000' }}>
                <CrownOutlined style={{ marginRight: 4 }} />
                {selectedCustomer.tier?.toUpperCase()} MEMBER
              </Tag>
            </div>

            <Divider>Recent Activity</Divider>
            <List
              size="small"
              dataSource={selectedCustomer.recent_activities || []}
              renderItem={item => (
                <List.Item>
                  <List.Item.Meta
                    avatar={<Avatar icon={<StarOutlined />} />}
                    title={item.activity}
                    description={moment(item.date).format('MMM DD, YYYY HH:mm')}
                  />
                  <div>{item.points} pts</div>
                </List.Item>
              )}
            />

            <div style={{ marginTop: 16, textAlign: 'right' }}>
              <Button type="primary" onClick={() => setIsCustomerModalVisible(false)}>
                Close
              </Button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
};

export default Loyalty;