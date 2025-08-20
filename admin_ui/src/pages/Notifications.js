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
  DatePicker,
  Row,
  Col,
  Statistic,
  message,
  Tabs,
  Badge,
  Divider,
  Switch,
  Radio,
  List,
  Avatar,
  Progress,
  Timeline
} from 'antd';
import {
  SearchOutlined,
  BellOutlined,
  MoreOutlined,
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  EyeOutlined,
  SendOutlined,
  MailOutlined,
  MessageOutlined,
  PhoneOutlined,
  ExportOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  ClockCircleOutlined,
  UserOutlined
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient } from 'react-query';
import moment from 'moment';
import adminService from '../services/adminService';

const { Option } = Select;
const { TextArea } = Input;
const { RangePicker } = DatePicker;

const Notifications = () => {
  const [activeTab, setActiveTab] = useState('campaigns');
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [typeFilter, setTypeFilter] = useState('');
  const [selectedCampaign, setSelectedCampaign] = useState(null);
  const [selectedTemplate, setSelectedTemplate] = useState(null);
  const [isCampaignModalVisible, setIsCampaignModalVisible] = useState(false);
  const [isTemplateModalVisible, setIsTemplateModalVisible] = useState(false);
  const [isEditModalVisible, setIsEditModalVisible] = useState(false);
  const [pagination, setPagination] = useState({ page: 1, per_page: 20 });
  const [campaignForm] = Form.useForm();
  const [templateForm] = Form.useForm();
  const [editForm] = Form.useForm();

  const queryClient = useQueryClient();

  // Fetch notification campaigns
  const { data: campaignsData, isLoading: campaignsLoading } = useQuery(
    ['notification-campaigns', pagination, searchText, statusFilter, typeFilter],
    () => adminService.getNotificationCampaigns({
      page: pagination.page,
      per_page: pagination.per_page,
      search: searchText,
      status: statusFilter,
      type: typeFilter
    }),
    {
      keepPreviousData: true
    }
  );

  // Fetch notification templates
  const { data: templatesData, isLoading: templatesLoading } = useQuery(
    ['notification-templates', pagination, searchText],
    () => adminService.getNotificationTemplates({
      page: pagination.page,
      per_page: pagination.per_page,
      search: searchText
    }),
    {
      keepPreviousData: true,
      enabled: activeTab === 'templates'
    }
  );

  // Create campaign mutation
  const createCampaignMutation = useMutation(
    (campaignData) => adminService.createNotificationCampaign(campaignData),
    {
      onSuccess: () => {
        message.success('Campaign created successfully');
        queryClient.invalidateQueries('notification-campaigns');
        setIsCampaignModalVisible(false);
        campaignForm.resetFields();
      },
      onError: (error) => {
        message.error('Failed to create campaign');
      }
    }
  );

  // Create template mutation
  const createTemplateMutation = useMutation(
    (templateData) => adminService.createNotificationTemplate(templateData),
    {
      onSuccess: () => {
        message.success('Template created successfully');
        queryClient.invalidateQueries('notification-templates');
        setIsTemplateModalVisible(false);
        templateForm.resetFields();
      },
      onError: (error) => {
        message.error('Failed to create template');
      }
    }
  );

  const campaignStatusColors = {
    draft: 'grey',
    scheduled: 'orange',
    sending: 'blue',
    sent: 'green',
    failed: 'red',
    cancelled: 'red'
  };

  const channelIcons = {
    email: <MailOutlined />,
    sms: <MessageOutlined />,
    push: <BellOutlined />,
    phone: <PhoneOutlined />
  };

  const campaignColumns = [
    {
      title: 'Campaign Name',
      dataIndex: 'name',
      key: 'name',
      render: (text, record) => (
        <div>
          <div style={{ fontWeight: 'bold' }}>{text}</div>
          <small style={{ color: '#666' }}>{record.subject}</small>
        </div>
      )
    },
    {
      title: 'Channel',
      dataIndex: 'channel',
      key: 'channel',
      width: 100,
      render: (channel) => (
        <Tag color="blue" icon={channelIcons[channel]}>
          {channel?.toUpperCase()}
        </Tag>
      )
    },
    {
      title: 'Recipients',
      dataIndex: 'recipient_count',
      key: 'recipient_count',
      width: 100,
      render: (count) => (
        <Badge count={count} style={{ backgroundColor: '#1890ff' }} />
      )
    },
    {
      title: 'Sent/Delivered',
      key: 'delivery_stats',
      width: 120,
      render: (_, record) => (
        <div>
          <div>{record.sent_count}/{record.recipient_count}</div>
          <Progress
            percent={record.recipient_count > 0 ? (record.sent_count / record.recipient_count) * 100 : 0}
            size="small"
            showInfo={false}
          />
        </div>
      )
    },
    {
      title: 'Status',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (status) => (
        <Tag color={campaignStatusColors[status] || 'default'}>
          {status?.toUpperCase()}
        </Tag>
      )
    },
    {
      title: 'Scheduled',
      dataIndex: 'scheduled_at',
      key: 'scheduled_at',
      width: 120,
      render: (date) => (date ? moment(date).format('MMM DD, YYYY HH:mm') : 'Immediate')
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
                onClick: () => handleViewCampaign(record)
              },
              {
                key: 'edit',
                label: 'Edit Campaign',
                icon: <EditOutlined />,
                disabled: record.status === 'sent' || record.status === 'sending',
                onClick: () => handleEditCampaign(record)
              },
              {
                type: 'divider'
              },
              {
                key: 'duplicate',
                label: 'Duplicate',
                onClick: () => handleDuplicateCampaign(record)
              },
              {
                key: 'delete',
                label: 'Delete Campaign',
                icon: <DeleteOutlined />,
                danger: true,
                disabled: record.status === 'sending',
                onClick: () => handleDeleteCampaign(record)
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

  const templateColumns = [
    {
      title: 'Template Name',
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
      title: 'Channel',
      dataIndex: 'channel',
      key: 'channel',
      width: 100,
      render: (channel) => (
        <Tag color="blue" icon={channelIcons[channel]}>
          {channel?.toUpperCase()}
        </Tag>
      )
    },
    {
      title: 'Category',
      dataIndex: 'category',
      key: 'category',
      width: 120,
      render: (category) => (
        <Tag color="green">{category}</Tag>
      )
    },
    {
      title: 'Usage Count',
      dataIndex: 'usage_count',
      key: 'usage_count',
      width: 100,
      render: (count) => (
        <span>{count} times</span>
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
                label: 'View Template',
                icon: <EyeOutlined />,
                onClick: () => handleViewTemplate(record)
              },
              {
                key: 'edit',
                label: 'Edit Template',
                icon: <EditOutlined />,
                onClick: () => handleEditTemplate(record)
              },
              {
                key: 'use',
                label: 'Use Template',
                icon: <SendOutlined />,
                onClick: () => handleUseTemplate(record)
              },
              {
                type: 'divider'
              },
              {
                key: 'delete',
                label: 'Delete Template',
                icon: <DeleteOutlined />,
                danger: true,
                onClick: () => handleDeleteTemplate(record)
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

  const handleViewCampaign = (campaign) => {
    setSelectedCampaign(campaign);
    // Implementation for viewing campaign details
  };

  const handleEditCampaign = (campaign) => {
    setSelectedCampaign(campaign);
    editForm.setFieldsValue({
      name: campaign.name,
      subject: campaign.subject,
      channel: campaign.channel,
      content: campaign.content,
      scheduled_at: campaign.scheduled_at ? moment(campaign.scheduled_at) : null
    });
    setIsEditModalVisible(true);
  };

  const handleDeleteCampaign = (campaign) => {
    Modal.confirm({
      title: 'Delete Campaign?',
      content: `Are you sure you want to delete "${campaign.name}"?`,
      onOk: () => {
        message.success('Campaign deleted successfully');
        queryClient.invalidateQueries('notification-campaigns');
      }
    });
  };

  const handleDuplicateCampaign = (campaign) => {
    const duplicatedCampaign = {
      ...campaign,
      name: `${campaign.name} (Copy)`,
      status: 'draft'
    };
    createCampaignMutation.mutate(duplicatedCampaign);
  };

  const handleViewTemplate = (template) => {
    setSelectedTemplate(template);
    // Implementation for viewing template details
  };

  const handleEditTemplate = (template) => {
    setSelectedTemplate(template);
    // Implementation for editing template
  };

  const handleDeleteTemplate = (template) => {
    Modal.confirm({
      title: 'Delete Template?',
      content: `Are you sure you want to delete "${template.name}"?`,
      onOk: () => {
        message.success('Template deleted successfully');
        queryClient.invalidateQueries('notification-templates');
      }
    });
  };

  const handleUseTemplate = (template) => {
    campaignForm.setFieldsValue({
      template_id: template.id,
      channel: template.channel,
      subject: template.subject,
      content: template.content
    });
    setIsCampaignModalVisible(true);
  };

  const handleCreateCampaign = (values) => {
    createCampaignMutation.mutate(values);
  };

  const handleCreateTemplate = (values) => {
    createTemplateMutation.mutate(values);
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
  const campaigns = campaignsData?.campaigns || [];
  const templates = templatesData?.templates || [];
  const totalCampaigns = campaignsData?.pagination?.total || 0;
  const activeCampaigns = campaigns.filter(c => c.status === 'sending' || c.status === 'scheduled').length;
  const totalSent = campaigns.reduce((sum, campaign) => sum + (campaign.sent_count || 0), 0);
  const totalRecipients = campaigns.reduce((sum, campaign) => sum + (campaign.recipient_count || 0), 0);
  const deliveryRate = totalRecipients > 0 ? ((totalSent / totalRecipients) * 100).toFixed(1) : 0;

  const tabItems = [
    {
      key: 'campaigns',
      label: 'Campaigns',
      children: (
        <div>
          {/* Summary Cards for Campaigns */}
          <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title="Total Campaigns"
                  value={totalCampaigns}
                  prefix={<BellOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title="Active Campaigns"
                  value={activeCampaigns}
                  valueStyle={{ color: '#1890ff' }}
                  prefix={<SendOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title="Messages Sent"
                  value={totalSent}
                  prefix={<CheckCircleOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={6}>
              <Card>
                <Statistic
                  title="Delivery Rate"
                  value={deliveryRate}
                  precision={1}
                  suffix="%"
                  valueStyle={{ color: '#52c41a' }}
                />
              </Card>
            </Col>
          </Row>

          <Card>
            {/* Filter Controls */}
            <div className="table-actions">
              <Space wrap>
                <Input.Search
                  placeholder="Search campaigns..."
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
                  <Option value="draft">Draft</Option>
                  <Option value="scheduled">Scheduled</Option>
                  <Option value="sending">Sending</Option>
                  <Option value="sent">Sent</Option>
                  <Option value="failed">Failed</Option>
                  <Option value="cancelled">Cancelled</Option>
                </Select>
                <Select
                  placeholder="Filter by channel"
                  allowClear
                  onChange={setTypeFilter}
                  style={{ width: 150 }}
                >
                  <Option value="email">Email</Option>
                  <Option value="sms">SMS</Option>
                  <Option value="push">Push</Option>
                  <Option value="phone">Phone</Option>
                </Select>
              </Space>

              <Space>
                <Button
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={() => setIsCampaignModalVisible(true)}
                >
                  Create Campaign
                </Button>
                <Button icon={<ExportOutlined />}>
                  Export Report
                </Button>
              </Space>
            </div>

            <Table
              columns={campaignColumns}
              dataSource={campaigns}
              loading={campaignsLoading}
              rowKey="id"
              pagination={{
                current: pagination.page,
                pageSize: pagination.per_page,
                total: totalCampaigns,
                showSizeChanger: true,
                showQuickJumper: true,
                showTotal: (total, range) =>
                  `${range[0]}-${range[1]} of ${total} campaigns`
              }}
              onChange={handleTableChange}
              className="admin-table"
            />
          </Card>
        </div>
      )
    },
    {
      key: 'templates',
      label: 'Templates',
      children: (
        <div>
          {/* Summary Cards for Templates */}
          <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title="Total Templates"
                  value={templatesData?.pagination?.total || 0}
                  prefix={<MailOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title="Email Templates"
                  value={templates.filter(t => t.channel === 'email').length}
                  prefix={<MailOutlined />}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card>
                <Statistic
                  title="SMS Templates"
                  value={templates.filter(t => t.channel === 'sms').length}
                  prefix={<MessageOutlined />}
                />
              </Card>
            </Col>
          </Row>

          <Card>
            <div className="table-actions">
              <Space wrap>
                <Input.Search
                  placeholder="Search templates..."
                  allowClear
                  onSearch={handleSearch}
                  style={{ width: 250 }}
                />
              </Space>

              <Space>
                <Button
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={() => setIsTemplateModalVisible(true)}
                >
                  Create Template
                </Button>
              </Space>
            </div>

            <Table
              columns={templateColumns}
              dataSource={templates}
              loading={templatesLoading}
              rowKey="id"
              pagination={{
                current: pagination.page,
                pageSize: pagination.per_page,
                total: templatesData?.pagination?.total || 0,
                showSizeChanger: true,
                showQuickJumper: true,
                showTotal: (total, range) =>
                  `${range[0]}-${range[1]} of ${total} templates`
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

      {/* Create Campaign Modal */}
      <Modal
        title="Create Notification Campaign"
        open={isCampaignModalVisible}
        onCancel={() => setIsCampaignModalVisible(false)}
        footer={null}
        width={700}
      >
        <Form
          form={campaignForm}
          layout="vertical"
          onFinish={handleCreateCampaign}
        >
          <Form.Item
            name="name"
            label="Campaign Name"
            rules={[{ required: true, message: 'Please enter campaign name' }]}
          >
            <Input placeholder="Enter campaign name" />
          </Form.Item>

          <Form.Item
            name="channel"
            label="Channel"
            rules={[{ required: true, message: 'Please select channel' }]}
          >
            <Radio.Group>
              <Radio.Button value="email"><MailOutlined /> Email</Radio.Button>
              <Radio.Button value="sms"><MessageOutlined /> SMS</Radio.Button>
              <Radio.Button value="push"><BellOutlined /> Push</Radio.Button>
              <Radio.Button value="phone"><PhoneOutlined /> Phone</Radio.Button>
            </Radio.Group>
          </Form.Item>

          <Form.Item
            name="subject"
            label="Subject"
            rules={[{ required: true, message: 'Please enter subject' }]}
          >
            <Input placeholder="Enter subject/title" />
          </Form.Item>

          <Form.Item
            name="content"
            label="Message Content"
            rules={[{ required: true, message: 'Please enter message content' }]}
          >
            <TextArea rows={6} placeholder="Enter your message content..." />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="target_audience"
                label="Target Audience"
                rules={[{ required: true, message: 'Please select audience' }]}
              >
                <Select placeholder="Select audience">
                  <Option value="all_customers">All Customers</Option>
                  <Option value="active_customers">Active Customers</Option>
                  <Option value="new_customers">New Customers</Option>
                  <Option value="loyalty_members">Loyalty Members</Option>
                  <Option value="custom_segment">Custom Segment</Option>
                </Select>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="priority"
                label="Priority"
              >
                <Select placeholder="Select priority">
                  <Option value="high">High</Option>
                  <Option value="medium">Medium</Option>
                  <Option value="low">Low</Option>
                </Select>
              </Form.Item>
            </Col>
          </Row>

          <Form.Item
            name="scheduled_at"
            label="Schedule (Optional)"
          >
            <DatePicker
              showTime
              placeholder="Send immediately if not scheduled"
              style={{ width: '100%' }}
            />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setIsCampaignModalVisible(false)}>
                Cancel
              </Button>
              <Button onClick={() => {
                campaignForm.setFieldsValue({ status: 'draft' });
                campaignForm.submit();
              }}>
                Save as Draft
              </Button>
              <Button
                type="primary"
                onClick={() => {
                  campaignForm.setFieldsValue({ status: 'scheduled' });
                  campaignForm.submit();
                }}
                loading={createCampaignMutation.isLoading}
              >
                Schedule Campaign
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* Create Template Modal */}
      <Modal
        title="Create Notification Template"
        open={isTemplateModalVisible}
        onCancel={() => setIsTemplateModalVisible(false)}
        footer={null}
        width={600}
      >
        <Form
          form={templateForm}
          layout="vertical"
          onFinish={handleCreateTemplate}
        >
          <Form.Item
            name="name"
            label="Template Name"
            rules={[{ required: true, message: 'Please enter template name' }]}
          >
            <Input placeholder="Enter template name" />
          </Form.Item>

          <Form.Item
            name="description"
            label="Description"
          >
            <Input placeholder="Enter template description" />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="channel"
                label="Channel"
                rules={[{ required: true, message: 'Please select channel' }]}
              >
                <Select placeholder="Select channel">
                  <Option value="email">Email</Option>
                  <Option value="sms">SMS</Option>
                  <Option value="push">Push</Option>
                  <Option value="phone">Phone</Option>
                </Select>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="category"
                label="Category"
                rules={[{ required: true, message: 'Please select category' }]}
              >
                <Select placeholder="Select category">
                  <Option value="promotional">Promotional</Option>
                  <Option value="transactional">Transactional</Option>
                  <Option value="reminder">Reminder</Option>
                  <Option value="alert">Alert</Option>
                </Select>
              </Form.Item>
            </Col>
          </Row>

          <Form.Item
            name="subject"
            label="Subject/Title"
            rules={[{ required: true, message: 'Please enter subject' }]}
          >
            <Input placeholder="Enter subject or title" />
          </Form.Item>

          <Form.Item
            name="content"
            label="Template Content"
            rules={[{ required: true, message: 'Please enter content' }]}
          >
            <TextArea rows={6} placeholder="Enter template content with variables like {{customer_name}}" />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setIsTemplateModalVisible(false)}>
                Cancel
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={createTemplateMutation.isLoading}
              >
                Create Template
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default Notifications;