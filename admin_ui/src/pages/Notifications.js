import React, { useEffect, useMemo, useState } from 'react';
import { DEFAULT_PAGE_SIZE } from '../utils/constants';
import {
  Badge,
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Divider,
  Drawer,
  Dropdown,
  Empty,
  Form,
  Input,
  List,
  Modal,
  Progress,
  Radio,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
  message
} from 'antd';
import {
  BellOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  CopyOutlined,
  DeleteOutlined,
  EditOutlined,
  ExperimentOutlined,
  ExportOutlined,
  EyeOutlined,
  InboxOutlined,
  MailOutlined,
  MessageOutlined,
  MoreOutlined,
  PlusOutlined,
  SaveOutlined,
  SearchOutlined,
  SendOutlined,
  StopOutlined
} from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import dayjs from 'dayjs';
import { formatDate, formatDateTime } from '../utils/dateUtils';
import adminService from '../services/adminService';
import exportUtils from '../utils/exportUtils';

const { Option } = Select;
const { RangePicker } = DatePicker;
const { Text, Paragraph } = Typography;

const CAMPAIGN_STATUS_COLORS = {
  draft: 'default',
  scheduled: 'gold',
  sending: 'processing',
  sent: 'success',
  failed: 'error',
  cancelled: 'error'
};

const CHANNEL_ICONS = {
  email: <MailOutlined />,
  sms: <MessageOutlined />,
  telegram: <SendOutlined />,
  in_app: <InboxOutlined />,
  push: <BellOutlined />
};

const PRIORITY_OPTIONS = [
  { value: 'low', label: 'Low' },
  { value: 'normal', label: 'Normal' },
  { value: 'high', label: 'High' },
  { value: 'urgent', label: 'Urgent' }
];

const AUDIENCE_OPTIONS = [
  { value: 'all_customers', label: 'All Customers' },
  { value: 'active_customers', label: 'Active Customers' },
  { value: 'new_customers', label: 'New Customers' },
  { value: 'loyalty_members', label: 'Loyalty Members' },
  { value: 'custom_segment', label: 'Custom Segment' }
];

const parseVariableJson = (rawValue) => {
  const value = `${rawValue || ''}`.trim();
  if (!value) {
    return {};
  }
  return JSON.parse(value);
};

const normalizeCampaignPayload = (values) => ({
  name: values.name?.trim(),
  template_id: values.template_id || null,
  notification_type: values.notification_type,
  channel: values.channel,
  subject: values.subject?.trim() || null,
  content: values.content?.trim() || null,
  target_audience: values.target_audience,
  target_segment_id: values.target_segment_id || null,
  specific_user_ids: (values.specific_user_ids || [])
    .map((value) => Number(value))
    .filter((value) => Number.isFinite(value)),
  priority: values.priority || 'normal',
  scheduled_at: values.scheduled_at ? values.scheduled_at.toISOString() : null
});

const normalizeTemplatePayload = (values) => ({
  name: values.name?.trim(),
  notification_type: values.notification_type,
  channel: values.channel,
  subject: values.subject?.trim() || null,
  content: values.content?.trim(),
  is_active: values.is_active !== false
});

const renderChannelTag = (channel, channels) => {
  const channelMeta = channels.find((item) => item.value === channel);
  return (
    // eslint-disable-next-line security/detect-object-injection
    <Tag color={channel === 'push' ? 'default' : 'blue'} icon={CHANNEL_ICONS[channel]}>
      {(channelMeta?.label || channel || '').toUpperCase()}
    </Tag>
  );
};

const Notifications = () => {
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState('campaigns');
  const [campaignPagination, setCampaignPagination] = useState({ page: 1, per_page: DEFAULT_PAGE_SIZE });
  const [templatePagination, setTemplatePagination] = useState({ page: 1, per_page: DEFAULT_PAGE_SIZE });
  const [campaignFilters, setCampaignFilters] = useState({
    search: '',
    status: undefined,
    channel: undefined,
    target_audience: undefined,
    dateRange: []
  });
  const [templateFilters, setTemplateFilters] = useState({
    search: '',
    channel: undefined,
    notification_type: undefined,
    is_active: undefined
  });
  const [campaignModalOpen, setCampaignModalOpen] = useState(false);
  const [templateModalOpen, setTemplateModalOpen] = useState(false);
  const [campaignDrawerOpen, setCampaignDrawerOpen] = useState(false);
  const [templateDrawerOpen, setTemplateDrawerOpen] = useState(false);
  const [campaignModalMode, setCampaignModalMode] = useState('create');
  const [templateModalMode, setTemplateModalMode] = useState('create');
  const [campaignSubmitMode, setCampaignSubmitMode] = useState('draft');
  const [editingCampaignId, setEditingCampaignId] = useState(null);
  const [editingTemplateId, setEditingTemplateId] = useState(null);
  const [selectedCampaignId, setSelectedCampaignId] = useState(null);
  const [selectedTemplateId, setSelectedTemplateId] = useState(null);
  const [templatePreviewLanguage, setTemplatePreviewLanguage] = useState('en');
  const [templatePreviewVariables, setTemplatePreviewVariables] = useState(
    JSON.stringify({ user_name: 'Admin User', order_number: 'BS-1001' }, null, 2)
  );

  const [campaignForm] = Form.useForm();
  const [templateForm] = Form.useForm();
  const campaignChannel = Form.useWatch('channel', campaignForm);
  const campaignAudience = Form.useWatch('target_audience', campaignForm);
  const templateChannel = Form.useWatch('channel', templateForm);

  const campaignQueryParams = useMemo(() => {
    const [startDate, endDate] = campaignFilters.dateRange || [];
    return {
      page: campaignPagination.page,
      per_page: campaignPagination.per_page,
      search: campaignFilters.search || undefined,
      status: campaignFilters.status || undefined,
      channel: campaignFilters.channel || undefined,
      target_audience: campaignFilters.target_audience || undefined,
      start_date: startDate ? startDate.startOf('day').toISOString() : undefined,
      end_date: endDate ? endDate.endOf('day').toISOString() : undefined
    };
  }, [campaignFilters, campaignPagination]);

  const templateQueryParams = useMemo(() => ({
    page: templatePagination.page,
    per_page: templatePagination.per_page,
    search: templateFilters.search || undefined,
    channel: templateFilters.channel || undefined,
    notification_type: templateFilters.notification_type || undefined,
    is_active: templateFilters.is_active
  }), [templateFilters, templatePagination]);

  const { data: campaignCollection, isLoading: campaignsLoading } = useQuery({
    queryKey: ['notification-campaigns', campaignQueryParams],
    queryFn: () => adminService.getNotificationCampaigns(campaignQueryParams),
    placeholderData: keepPreviousData,
  });

  const { data: templateCollection, isLoading: templatesLoading } = useQuery({
    queryKey: ['notification-templates', templateQueryParams],
    queryFn: () => adminService.getNotificationTemplates(templateQueryParams),
    placeholderData: keepPreviousData,
    enabled: activeTab === 'templates' || templateDrawerOpen || templateModalOpen || campaignModalOpen,
  });

  const { data: selectedCampaign, isFetching: campaignDetailLoading } = useQuery({
    queryKey: ['notification-campaign-detail', selectedCampaignId],
    queryFn: () => adminService.getNotificationCampaign(selectedCampaignId),
    enabled: Boolean(selectedCampaignId),
  });

  const { data: selectedTemplate, isFetching: templateDetailLoading } = useQuery({
    queryKey: ['notification-template-detail', selectedTemplateId],
    queryFn: () => adminService.getNotificationTemplate(selectedTemplateId),
    enabled: Boolean(selectedTemplateId),
  });

  const { data: notificationTypes = [] } = useQuery({
    queryKey: ['notification-template-types'],
    queryFn: () => adminService.getNotificationTemplateTypes(),
  });

  const { data: channelOptions = [] } = useQuery({
    queryKey: ['notification-template-channels'],
    queryFn: () => adminService.getNotificationTemplateChannels(),
  });

  const { data: segmentOptions = [] } = useQuery({
    queryKey: ['notification-campaign-segments'],
    queryFn: () => adminService.getNotificationCampaignSegments(),
  });

  const templatePreviewMutation = useMutation({
    mutationFn: ({ templateId, payload }) => adminService.previewNotificationTemplate(templateId, payload),

    onError: (error) => {
      message.error(error?.response?.data?.message || 'Failed to render template preview');
    },
  });

  const templateTestSendMutation = useMutation({
    mutationFn: ({ templateId, payload }) => adminService.testSendNotificationTemplate(templateId, payload),

    onSuccess: () => {
      message.success('Template test notification sent');
    },

    onError: (error) => {
      message.error(error?.response?.data?.message || 'Failed to send test notification');
    },
  });

  const campaignSaveMutation = useMutation({
    mutationFn: async ({ values, mode }) => {
      const payload = normalizeCampaignPayload(values);
      let campaign;

      if (editingCampaignId) {
        campaign = await adminService.updateNotificationCampaign(editingCampaignId, payload);
      } else {
        campaign = await adminService.createNotificationCampaign(payload);
      }

      if (mode === 'send_now') {
        campaign = await adminService.sendNotificationCampaign(campaign.id, { send_now: true });
      }
      if (mode === 'schedule') {
        if (!payload.scheduled_at) {
          throw new Error('Schedule time is required');
        }
        campaign = await adminService.sendNotificationCampaign(campaign.id, { send_now: false });
      }

      return campaign;
    },

    onSuccess: (_, variables) => {
      message.success(
        variables.mode === 'draft'
          ? 'Campaign saved'
          : variables.mode === 'send_now'
            ? 'Campaign sent'
            : 'Campaign scheduled'
      );
      queryClient.invalidateQueries({
        queryKey: ['notification-campaigns'],
      });
      if (selectedCampaignId) {
        queryClient.invalidateQueries({
          queryKey: ['notification-campaign-detail', selectedCampaignId],
        });
      }
      setCampaignModalOpen(false);
      setEditingCampaignId(null);
      campaignForm.resetFields();
    },

    onError: (error) => {
      message.error(error?.response?.data?.message || error?.message || 'Failed to save campaign');
    },
  });

  const campaignDeleteMutation = useMutation({
    mutationFn: (campaignId) => adminService.deleteNotificationCampaign(campaignId),

    onSuccess: () => {
      message.success('Campaign deleted');
      queryClient.invalidateQueries({
        queryKey: ['notification-campaigns'],
      });
      setCampaignDrawerOpen(false);
      setSelectedCampaignId(null);
    },

    onError: (error) => {
      message.error(error?.response?.data?.message || 'Failed to delete campaign');
    },
  });

  const campaignDuplicateMutation = useMutation({
    mutationFn: (campaignId) => adminService.duplicateNotificationCampaign(campaignId),

    onSuccess: () => {
      message.success('Campaign duplicated');
      queryClient.invalidateQueries({
        queryKey: ['notification-campaigns'],
      });
    },

    onError: (error) => {
      message.error(error?.response?.data?.message || 'Failed to duplicate campaign');
    },
  });

  const campaignCancelMutation = useMutation({
    mutationFn: (campaignId) => adminService.cancelNotificationCampaign(campaignId),

    onSuccess: (_, campaignId) => {
      message.success('Campaign cancelled');
      queryClient.invalidateQueries({
        queryKey: ['notification-campaigns'],
      });
      queryClient.invalidateQueries({
        queryKey: ['notification-campaign-detail', campaignId],
      });
    },

    onError: (error) => {
      message.error(error?.response?.data?.message || 'Failed to cancel campaign');
    },
  });

  const templateSaveMutation = useMutation({
    mutationFn: async (values) => {
      const payload = normalizeTemplatePayload(values);
      if (editingTemplateId) {
        return adminService.updateNotificationTemplate(editingTemplateId, payload);
      }
      return adminService.createNotificationTemplate(payload);
    },

    onSuccess: () => {
      message.success(editingTemplateId ? 'Template updated' : 'Template created');
      queryClient.invalidateQueries({
        queryKey: ['notification-templates'],
      });
      if (selectedTemplateId) {
        queryClient.invalidateQueries({
          queryKey: ['notification-template-detail', selectedTemplateId],
        });
      }
      setTemplateModalOpen(false);
      setEditingTemplateId(null);
      templateForm.resetFields();
    },

    onError: (error) => {
      message.error(error?.response?.data?.message || 'Failed to save template');
    },
  });

  const templateToggleMutation = useMutation({
    mutationFn: ({ templateId, isActive }) => adminService.updateNotificationTemplate(templateId, { is_active: isActive }),

    onSuccess: () => {
      message.success('Template status updated');
      queryClient.invalidateQueries({
        queryKey: ['notification-templates'],
      });
      if (selectedTemplateId) {
        queryClient.invalidateQueries({
          queryKey: ['notification-template-detail', selectedTemplateId],
        });
      }
    },

    onError: (error) => {
      message.error(error?.response?.data?.message || 'Failed to update template status');
    },
  });

  const runTemplatePreview = async (templateId) => {
    try {
      const preview = await templatePreviewMutation.mutateAsync({
        templateId,
        payload: {
          language: templatePreviewLanguage,
          variables: parseVariableJson(templatePreviewVariables)
        }
      });
      return preview;
    } catch (error) {
      return null;
    }
  };

  useEffect(() => {
    if (selectedTemplateId && templateDrawerOpen) {
      runTemplatePreview(selectedTemplateId);
    }
  }, [selectedTemplateId, templateDrawerOpen, templatePreviewLanguage]); // eslint-disable-line react-hooks/exhaustive-deps

  const campaigns = campaignCollection?.campaigns || [];
  const templates = templateCollection?.templates || [];
  const availableChannels = channelOptions.filter((channel) => channel.available !== false || channel.value === 'push');
  const creatableChannels = channelOptions.filter((channel) => channel.available !== false);
  const totalCampaigns = campaignCollection?.pagination?.total || 0;
  const activeCampaigns = campaigns.filter((campaign) => ['scheduled', 'sending'].includes(campaign.status)).length;
  const totalSent = campaigns.reduce((sum, campaign) => sum + (campaign.sent_count || 0), 0);
  const totalRecipients = campaigns.reduce((sum, campaign) => sum + (campaign.recipient_count || 0), 0);
  const deliveryRate = totalRecipients > 0 ? (totalSent / totalRecipients) * 100 : 0;

  const openCampaignModal = (mode = 'create') => {
    setCampaignModalMode(mode);
    setCampaignModalOpen(true);
  };

  const openTemplateModal = (mode = 'create') => {
    setTemplateModalMode(mode);
    setTemplateModalOpen(true);
  };

  const handleCreateCampaign = () => {
    setEditingCampaignId(null);
    campaignForm.resetFields();
    campaignForm.setFieldsValue({
      priority: 'normal',
      target_audience: 'all_customers'
    });
    openCampaignModal('create');
  };

  const handleViewCampaign = (campaign) => {
    setSelectedCampaignId(campaign.id);
    setCampaignDrawerOpen(true);
  };

  const handleEditCampaign = async (campaign) => {
    const detail = await adminService.getNotificationCampaign(campaign.id);
    setEditingCampaignId(detail.id);
    campaignForm.setFieldsValue({
      name: detail.name,
      template_id: detail.template_id || undefined,
      notification_type: detail.notification_type,
      channel: detail.channel,
      subject: detail.subject || '',
      content: detail.content || '',
      target_audience: detail.target_audience,
      target_segment_id: detail.target_segment_id || undefined,
      specific_user_ids: (detail.specific_user_ids || []).map(String),
      priority: detail.priority || 'normal',
      scheduled_at: detail.scheduled_at ? dayjs(detail.scheduled_at) : null
    });
    openCampaignModal('edit');
  };

  const handleDuplicateCampaign = (campaign) => {
    campaignDuplicateMutation.mutate(campaign.id);
  };

  const handleCreateTemplate = () => {
    setEditingTemplateId(null);
    templateForm.resetFields();
    templateForm.setFieldsValue({ is_active: true });
    openTemplateModal('create');
  };

  const handleViewTemplate = (template) => {
    setSelectedTemplateId(template.id);
    setTemplateDrawerOpen(true);
  };

  const handleEditTemplate = async (template) => {
    const detail = await adminService.getNotificationTemplate(template.id);
    setEditingTemplateId(detail.id);
    templateForm.setFieldsValue({
      name: detail.name,
      notification_type: detail.notification_type,
      channel: detail.channel,
      subject: detail.subject || '',
      content: detail.content || '',
      is_active: detail.is_active !== false
    });
    openTemplateModal('edit');
  };

  const handleUseTemplate = async (template) => {
    const detail = template.notification_type ? template : await adminService.getNotificationTemplate(template.id);
    setEditingCampaignId(null);
    campaignForm.resetFields();
    campaignForm.setFieldsValue({
      name: `${detail.name} Campaign`,
      template_id: detail.id,
      notification_type: detail.notification_type,
      channel: detail.channel,
      subject: detail.subject || '',
      content: detail.content || '',
      target_audience: 'all_customers',
      priority: 'normal'
    });
    setActiveTab('campaigns');
    openCampaignModal('create');
  };

  const handleCampaignSubmit = (values) => {
    campaignSaveMutation.mutate({ values, mode: campaignSubmitMode });
  };

  const handleTemplateSubmit = (values) => {
    templateSaveMutation.mutate(values);
  };

  const handleExportCampaigns = async () => {
    const result = await exportUtils.exportNotificationCampaigns(campaignQueryParams);
    if (result?.success === false) {
      message.error(result.message || 'Export failed');
      return;
    }
    message.success('Campaign export generated');
  };

  const campaignColumns = [
    {
      title: 'Campaign',
      dataIndex: 'name',
      key: 'name',
      render: (_, record) => (
        <div>
          <div style={{ fontWeight: 600 }}>{record.name}</div>
          <Text type="secondary">{record.subject || record.content || 'No custom content'}</Text>
        </div>
      )
    },
    {
      title: 'Type',
      dataIndex: 'notification_type',
      key: 'notification_type',
      render: (value, record) => (
        <Space direction="vertical" size={2}>
          <Text>{value?.replace(/_/g, ' ')}</Text>
          <Tag>{record.category}</Tag>
        </Space>
      )
    },
    {
      title: 'Channel',
      dataIndex: 'channel',
      key: 'channel',
      render: (value) => renderChannelTag(value, availableChannels)
    },
    {
      title: 'Recipients',
      dataIndex: 'recipient_count',
      key: 'recipient_count',
      render: (count) => <Badge count={count} style={{ backgroundColor: '#1677ff' }} />
    },
    {
      title: 'Delivery',
      key: 'delivery',
      render: (_, record) => (
        <div style={{ minWidth: 120 }}>
          <div>{record.sent_count}/{record.recipient_count}</div>
          <Progress
            percent={record.recipient_count ? (record.sent_count / record.recipient_count) * 100 : 0}
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
      // eslint-disable-next-line security/detect-object-injection
      render: (status) => <Tag color={CAMPAIGN_STATUS_COLORS[status]}>{status?.toUpperCase()}</Tag>
    },
    {
      title: 'Schedule',
      dataIndex: 'scheduled_at',
      key: 'scheduled_at',
      render: (value) => value ? formatDateTime(value) : 'Immediate'
    },
    {
      title: 'Actions',
      key: 'actions',
      render: (_, record) => (
        <Dropdown
          trigger={['click']}
          menu={{
            items: [
              { key: 'view', label: 'View', icon: <EyeOutlined />, onClick: () => handleViewCampaign(record) },
              {
                key: 'edit',
                label: 'Edit',
                icon: <EditOutlined />,
                disabled: !['draft', 'scheduled'].includes(record.status),
                onClick: () => handleEditCampaign(record)
              },
              { key: 'duplicate', label: 'Duplicate', icon: <CopyOutlined />, onClick: () => handleDuplicateCampaign(record) },
              {
                key: 'cancel',
                label: 'Cancel',
                icon: <StopOutlined />,
                disabled: !['scheduled', 'sending'].includes(record.status),
                onClick: () => campaignCancelMutation.mutate(record.id)
              },
              {
                key: 'delete',
                label: 'Delete',
                icon: <DeleteOutlined />,
                danger: true,
                disabled: !['draft', 'cancelled'].includes(record.status),
                onClick: () => campaignDeleteMutation.mutate(record.id)
              }
            ]
          }}
        >
          <Button type="text" icon={<MoreOutlined />} />
        </Dropdown>
      )
    }
  ];

  const templateColumns = [
    {
      title: 'Template',
      dataIndex: 'name',
      key: 'name',
      render: (_, record) => (
        <div>
          <div style={{ fontWeight: 600 }}>{record.name}</div>
          <Text type="secondary">{record.description || record.content}</Text>
        </div>
      )
    },
    {
      title: 'Type',
      dataIndex: 'notification_type',
      key: 'notification_type',
      render: (value, record) => (
        <Space direction="vertical" size={2}>
          <Text>{value?.replace(/_/g, ' ')}</Text>
          <Tag>{record.category}</Tag>
        </Space>
      )
    },
    {
      title: 'Channel',
      dataIndex: 'channel',
      key: 'channel',
      render: (value) => renderChannelTag(value, availableChannels)
    },
    {
      title: 'Usage',
      dataIndex: 'usage_count',
      key: 'usage_count',
      render: (value) => `${value} campaign(s)`
    },
    {
      title: 'Status',
      dataIndex: 'is_active',
      key: 'is_active',
      render: (value) => <Tag color={value ? 'success' : 'default'}>{value ? 'ACTIVE' : 'INACTIVE'}</Tag>
    },
    {
      title: 'Updated',
      dataIndex: 'updated_at',
      key: 'updated_at',
      render: (value, record) => formatDate(value || record.created_at)
    },
    {
      title: 'Actions',
      key: 'actions',
      render: (_, record) => (
        <Dropdown
          trigger={['click']}
          menu={{
            items: [
              { key: 'view', label: 'View', icon: <EyeOutlined />, onClick: () => handleViewTemplate(record) },
              { key: 'edit', label: 'Edit', icon: <EditOutlined />, onClick: () => handleEditTemplate(record) },
              { key: 'use', label: 'Use in Campaign', icon: <SendOutlined />, onClick: () => handleUseTemplate(record) },
              {
                key: 'toggle',
                label: record.is_active ? 'Deactivate' : 'Activate',
                icon: record.is_active ? <DeleteOutlined /> : <CheckCircleOutlined />,
                onClick: () => templateToggleMutation.mutate({ templateId: record.id, isActive: !record.is_active })
              }
            ]
          }}
        >
          <Button type="text" icon={<MoreOutlined />} />
        </Dropdown>
      )
    }
  ];

  const templatePreview = templatePreviewMutation.data;

  return (
    <div>
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          {
            key: 'campaigns',
            label: 'Campaigns',
            children: (
              <>
                <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
                  <Col xs={24} sm={12} lg={6}>
                    <Card><Statistic title="Total Campaigns" value={totalCampaigns} prefix={<BellOutlined />} /></Card>
                  </Col>
                  <Col xs={24} sm={12} lg={6}>
                    <Card><Statistic title="Active Campaigns" value={activeCampaigns} prefix={<ClockCircleOutlined />} /></Card>
                  </Col>
                  <Col xs={24} sm={12} lg={6}>
                    <Card><Statistic title="Messages Sent" value={totalSent} prefix={<CheckCircleOutlined />} /></Card>
                  </Col>
                  <Col xs={24} sm={12} lg={6}>
                    <Card><Statistic title="Delivery Rate" value={deliveryRate} precision={1} suffix="%" /></Card>
                  </Col>
                </Row>

                <Card>
                  <div className="table-actions">
                    <Space wrap>
                      <Input.Search
                        allowClear
                        placeholder="Search campaigns"
                        prefix={<SearchOutlined />}
                        style={{ width: 260 }}
                        onSearch={(value) => {
                          setCampaignPagination((prev) => ({ ...prev, page: 1 }));
                          setCampaignFilters((prev) => ({ ...prev, search: value }));
                        }}
                      />
                      <Select
                        allowClear
                        placeholder="Status"
                        style={{ width: 140 }}
                        value={campaignFilters.status}
                        onChange={(value) => {
                          setCampaignPagination((prev) => ({ ...prev, page: 1 }));
                          setCampaignFilters((prev) => ({ ...prev, status: value }));
                        }}
                      >
                        {Object.keys(CAMPAIGN_STATUS_COLORS).map((status) => (
                          <Option key={status} value={status}>{status}</Option>
                        ))}
                      </Select>
                      <Select
                        allowClear
                        placeholder="Channel"
                        style={{ width: 160 }}
                        value={campaignFilters.channel}
                        onChange={(value) => {
                          setCampaignPagination((prev) => ({ ...prev, page: 1 }));
                          setCampaignFilters((prev) => ({ ...prev, channel: value }));
                        }}
                      >
                        {channelOptions.map((channel) => (
                          <Option key={channel.value} value={channel.value}>
                            {channel.label}
                          </Option>
                        ))}
                      </Select>
                      <Select
                        allowClear
                        placeholder="Audience"
                        style={{ width: 180 }}
                        value={campaignFilters.target_audience}
                        onChange={(value) => {
                          setCampaignPagination((prev) => ({ ...prev, page: 1 }));
                          setCampaignFilters((prev) => ({ ...prev, target_audience: value }));
                        }}
                      >
                        {AUDIENCE_OPTIONS.map((audience) => (
                          <Option key={audience.value} value={audience.value}>{audience.label}</Option>
                        ))}
                      </Select>
                      <RangePicker
                        value={campaignFilters.dateRange}
                        onChange={(value) => {
                          setCampaignPagination((prev) => ({ ...prev, page: 1 }));
                          setCampaignFilters((prev) => ({ ...prev, dateRange: value || [] }));
                        }}
                      />
                    </Space>

                    <Space>
                      <Button type="primary" icon={<PlusOutlined />} onClick={handleCreateCampaign}>
                        Create Campaign
                      </Button>
                      <Button icon={<ExportOutlined />} onClick={handleExportCampaigns}>
                        Export
                      </Button>
                    </Space>
                  </div>

                  <Table
                    rowKey="id"
                    columns={campaignColumns}
                    dataSource={campaigns}
                    loading={campaignsLoading}
                    pagination={{
                      current: campaignPagination.page,
                      pageSize: campaignPagination.per_page,
                      total: totalCampaigns,
                      showSizeChanger: true,
                      showQuickJumper: true
                    }}
                    onChange={(pagination) => {
                      setCampaignPagination({
                        page: pagination.current,
                        per_page: pagination.pageSize
                      });
                    }}
                  />
                </Card>
              </>
            )
          },
          {
            key: 'templates',
            label: 'Templates',
            children: (
              <>
                <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
                  <Col xs={24} sm={8}>
                    <Card><Statistic title="Total Templates" value={templateCollection?.pagination?.total || 0} prefix={<MailOutlined />} /></Card>
                  </Col>
                  <Col xs={24} sm={8}>
                    <Card><Statistic title="Telegram Templates" value={templates.filter((item) => item.channel === 'telegram').length} prefix={<SendOutlined />} /></Card>
                  </Col>
                  <Col xs={24} sm={8}>
                    <Card><Statistic title="Inactive Templates" value={templates.filter((item) => item.is_active === false).length} prefix={<ClockCircleOutlined />} /></Card>
                  </Col>
                </Row>

                <Card>
                  <div className="table-actions">
                    <Space wrap>
                      <Input.Search
                        allowClear
                        placeholder="Search templates"
                        style={{ width: 260 }}
                        onSearch={(value) => {
                          setTemplatePagination((prev) => ({ ...prev, page: 1 }));
                          setTemplateFilters((prev) => ({ ...prev, search: value }));
                        }}
                      />
                      <Select
                        allowClear
                        placeholder="Channel"
                        style={{ width: 160 }}
                        value={templateFilters.channel}
                        onChange={(value) => {
                          setTemplatePagination((prev) => ({ ...prev, page: 1 }));
                          setTemplateFilters((prev) => ({ ...prev, channel: value }));
                        }}
                      >
                        {channelOptions.map((channel) => (
                          <Option key={channel.value} value={channel.value}>{channel.label}</Option>
                        ))}
                      </Select>
                      <Select
                        allowClear
                        placeholder="Notification Type"
                        style={{ width: 220 }}
                        value={templateFilters.notification_type}
                        onChange={(value) => {
                          setTemplatePagination((prev) => ({ ...prev, page: 1 }));
                          setTemplateFilters((prev) => ({ ...prev, notification_type: value }));
                        }}
                      >
                        {notificationTypes.map((type) => (
                          <Option key={type.value} value={type.value}>{type.label}</Option>
                        ))}
                      </Select>
                      <Select
                        allowClear
                        placeholder="Status"
                        style={{ width: 140 }}
                        value={templateFilters.is_active}
                        onChange={(value) => {
                          setTemplatePagination((prev) => ({ ...prev, page: 1 }));
                          setTemplateFilters((prev) => ({ ...prev, is_active: value }));
                        }}
                      >
                        <Option value={true}>Active</Option>
                        <Option value={false}>Inactive</Option>
                      </Select>
                    </Space>

                    <Button type="primary" icon={<PlusOutlined />} onClick={handleCreateTemplate}>
                      Create Template
                    </Button>
                  </div>

                  <Table
                    rowKey="id"
                    columns={templateColumns}
                    dataSource={templates}
                    loading={templatesLoading}
                    pagination={{
                      current: templatePagination.page,
                      pageSize: templatePagination.per_page,
                      total: templateCollection?.pagination?.total || 0,
                      showSizeChanger: true,
                      showQuickJumper: true
                    }}
                    onChange={(pagination) => {
                      setTemplatePagination({
                        page: pagination.current,
                        per_page: pagination.pageSize
                      });
                    }}
                  />
                </Card>
              </>
            )
          }
        ]}
      />
      <Modal
        title={campaignModalMode === 'edit' ? 'Edit Campaign' : 'Create Campaign'}
        open={campaignModalOpen}
        onCancel={() => {
          setCampaignModalOpen(false);
          setEditingCampaignId(null);
        }}
        footer={null}
        width={860}
        destroyOnHidden
      >
        <Form form={campaignForm} layout="vertical" onFinish={handleCampaignSubmit}>
          <Row gutter={16}>
            <Col span={14}>
              <Form.Item name="name" label="Campaign Name" rules={[{ required: true, message: 'Campaign name is required' }]}>
                <Input placeholder="Retention push for dormant customers" />
              </Form.Item>
            </Col>
            <Col span={10}>
              <Form.Item name="template_id" label="Template">
                <Select allowClear placeholder="Optional saved template">
                  {templates
                    .filter((template) => template.is_active !== false)
                    .map((template) => (
                      <Option key={template.id} value={template.id}>{template.name}</Option>
                    ))}
                </Select>
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="notification_type" label="Notification Type" rules={[{ required: true, message: 'Select a notification type' }]}>
                <Select showSearch optionFilterProp="children" placeholder="Select notification type">
                  {notificationTypes.map((type) => (
                    <Option key={type.value} value={type.value}>
                      {type.label}
                    </Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="channel" label="Channel" rules={[{ required: true, message: 'Select a channel' }]}>
                <Radio.Group buttonStyle="solid">
                  {creatableChannels.map((channel) => (
                    <Radio.Button key={channel.value} value={channel.value}>
                      <Space size={6}>
                        {CHANNEL_ICONS[channel.value]}
                        {channel.label}
                      </Space>
                    </Radio.Button>
                  ))}
                </Radio.Group>
              </Form.Item>
            </Col>
          </Row>

          <Form.Item
            noStyle
            shouldUpdate={(prev, next) => prev.channel !== next.channel || prev.template_id !== next.template_id}
          >
            {({ getFieldValue }) => {
              const requiresSubject = campaignChannel === 'email' && !getFieldValue('template_id');
              return (
                <Form.Item
                  name="subject"
                  label="Subject / Title"
                  rules={requiresSubject ? [{ required: true, message: 'Subject is required for email campaigns without a template' }] : []}
                >
                  <Input placeholder="Optional override" />
                </Form.Item>
              );
            }}
          </Form.Item>

          <Form.Item
            noStyle
            shouldUpdate={(prev, next) => prev.template_id !== next.template_id}
          >
            {({ getFieldValue }) => {
              const templateSelected = Boolean(getFieldValue('template_id'));
              return (
                <Form.Item
                  name="content"
                  label="Message Content"
                  rules={!templateSelected ? [{ required: true, message: 'Message content is required when no template is selected' }] : []}
                >
                  <Input.TextArea rows={6} placeholder="Use {user_name}, {order_number}, {company_name} and other placeholders" />
                </Form.Item>
              );
            }}
          </Form.Item>

          <Row gutter={16}>
            <Col span={8}>
              <Form.Item name="target_audience" label="Target Audience" rules={[{ required: true, message: 'Select an audience' }]}>
                <Select placeholder="Audience">
                  {AUDIENCE_OPTIONS.map((option) => (
                    <Option key={option.value} value={option.value}>{option.label}</Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="priority" label="Priority">
                <Select placeholder="Priority">
                  {PRIORITY_OPTIONS.map((option) => (
                    <Option key={option.value} value={option.value}>{option.label}</Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="scheduled_at" label="Schedule">
                <DatePicker showTime style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          {campaignAudience === 'custom_segment' && (
            <Row gutter={16}>
              <Col span={12}>
                <Form.Item name="target_segment_id" label="User Segment">
                  <Select allowClear placeholder="Optional saved segment">
                    {segmentOptions.map((segment) => (
                      <Option key={segment.id} value={segment.id}>
                        {segment.name} ({segment.user_count || 0})
                      </Option>
                    ))}
                  </Select>
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item name="specific_user_ids" label="Specific User IDs">
                  <Select
                    mode="tags"
                    tokenSeparators={[',', ' ']}
                    placeholder="Enter user IDs if needed"
                    open={false}
                  />
                </Form.Item>
              </Col>
            </Row>
          )}

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setCampaignModalOpen(false)}>Close</Button>
              <Button
                icon={<SaveOutlined />}
                loading={campaignSaveMutation.isPending && campaignSubmitMode === 'draft'}
                onClick={() => {
                  setCampaignSubmitMode('draft');
                  campaignForm.submit();
                }}
              >
                Save Draft
              </Button>
              <Button
                icon={<SendOutlined />}
                loading={campaignSaveMutation.isPending && campaignSubmitMode === 'send_now'}
                onClick={() => {
                  setCampaignSubmitMode('send_now');
                  campaignForm.submit();
                }}
              >
                Send Now
              </Button>
              <Button
                type="primary"
                icon={<ClockCircleOutlined />}
                loading={campaignSaveMutation.isPending && campaignSubmitMode === 'schedule'}
                onClick={() => {
                  setCampaignSubmitMode('schedule');
                  campaignForm.submit();
                }}
              >
                Schedule
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title={templateModalMode === 'edit' ? 'Edit Template' : 'Create Template'}
        open={templateModalOpen}
        onCancel={() => {
          setTemplateModalOpen(false);
          setEditingTemplateId(null);
        }}
        footer={null}
        width={760}
        destroyOnHidden
      >
        <Form form={templateForm} layout="vertical" onFinish={handleTemplateSubmit}>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="name" label="Template Name" rules={[{ required: true, message: 'Template name is required' }]}>
                <Input placeholder="Telegram delivery reminder" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="notification_type" label="Notification Type" rules={[{ required: true, message: 'Select a notification type' }]}>
                <Select showSearch optionFilterProp="children">
                  {notificationTypes.map((type) => (
                    <Option key={type.value} value={type.value}>{type.label}</Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="channel" label="Channel" rules={[{ required: true, message: 'Select a channel' }]}>
                <Select>
                  {creatableChannels.map((channel) => (
                    <Option key={channel.value} value={channel.value}>{channel.label}</Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="is_active" label="Active" valuePropName="checked">
                <Switch />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item
            name="subject"
            label="Subject / Title"
            rules={templateChannel === 'email' ? [{ required: true, message: 'Subject is required for email templates' }] : []}
          >
            <Input placeholder="Optional for non-email channels" />
          </Form.Item>

          <Form.Item name="content" label="Template Content" rules={[{ required: true, message: 'Template content is required' }]}>
            <Input.TextArea rows={8} placeholder="Use {user_name}, {order_number}, {company_name} and other placeholders" />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setTemplateModalOpen(false)}>Close</Button>
              <Button type="primary" loading={templateSaveMutation.isPending} htmlType="submit">
                {templateModalMode === 'edit' ? 'Update Template' : 'Create Template'}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
      <Drawer
        title={selectedCampaign?.name || 'Campaign Details'}
        width={760}
        open={campaignDrawerOpen}
        onClose={() => {
          setCampaignDrawerOpen(false);
          setSelectedCampaignId(null);
        }}
      >
        {campaignDetailLoading && !selectedCampaign ? (
          <Card loading />
        ) : selectedCampaign ? (
          <>
            <Space style={{ marginBottom: 16 }} wrap>
              <Tag color={CAMPAIGN_STATUS_COLORS[selectedCampaign.status]}>{selectedCampaign.status?.toUpperCase()}</Tag>
              {renderChannelTag(selectedCampaign.channel, availableChannels)}
              <Tag>{selectedCampaign.notification_type?.replace(/_/g, ' ')}</Tag>
              <Button icon={<EditOutlined />} disabled={!['draft', 'scheduled'].includes(selectedCampaign.status)} onClick={() => handleEditCampaign(selectedCampaign)}>
                Edit
              </Button>
              <Button icon={<CopyOutlined />} onClick={() => handleDuplicateCampaign(selectedCampaign)}>
                Duplicate
              </Button>
              <Button
                icon={<SendOutlined />}
                disabled={!['draft'].includes(selectedCampaign.status)}
                onClick={() => adminService.sendNotificationCampaign(selectedCampaign.id, { send_now: true }).then(() => {
                  message.success('Campaign queued');
                  queryClient.invalidateQueries({
                    queryKey: ['notification-campaigns'],
                  });
                  queryClient.invalidateQueries({
                    queryKey: ['notification-campaign-detail', selectedCampaign.id],
                  });
                }).catch((error) => {
                  message.error(error?.response?.data?.message || 'Failed to queue campaign');
                })}
              >
                Send Now
              </Button>
              <Button
                icon={<StopOutlined />}
                disabled={!['scheduled', 'sending'].includes(selectedCampaign.status)}
                onClick={() => campaignCancelMutation.mutate(selectedCampaign.id)}
              >
                Cancel
              </Button>
            </Space>

            <Descriptions bordered column={2} size="small">
              <Descriptions.Item label="Audience">{selectedCampaign.target_audience}</Descriptions.Item>
              <Descriptions.Item label="Priority">{selectedCampaign.priority}</Descriptions.Item>
              <Descriptions.Item label="Scheduled">{selectedCampaign.scheduled_at ? formatDateTime(selectedCampaign.scheduled_at) : 'Immediate'}</Descriptions.Item>
              <Descriptions.Item label="Queued">{selectedCampaign.queued_at ? formatDateTime(selectedCampaign.queued_at) : 'Not queued'}</Descriptions.Item>
              <Descriptions.Item label="Started">{selectedCampaign.started_at ? formatDateTime(selectedCampaign.started_at) : 'Not started'}</Descriptions.Item>
              <Descriptions.Item label="Completed">{selectedCampaign.completed_at ? formatDateTime(selectedCampaign.completed_at) : 'Not completed'}</Descriptions.Item>
              <Descriptions.Item label="Template">{selectedCampaign.template?.name || 'Custom content'}</Descriptions.Item>
              <Descriptions.Item label="Recipients">{selectedCampaign.recipient_count}</Descriptions.Item>
              <Descriptions.Item label="Created">{formatDateTime(selectedCampaign.created_at)}</Descriptions.Item>
              <Descriptions.Item label="Updated">{formatDateTime(selectedCampaign.updated_at)}</Descriptions.Item>
            </Descriptions>

            <Divider />

            <Row gutter={[16, 16]}>
              <Col span={8}>
                <Card><Statistic title="Sent" value={selectedCampaign.summary?.sent || selectedCampaign.sent_count || 0} /></Card>
              </Col>
              <Col span={8}>
                <Card><Statistic title="Delivered" value={selectedCampaign.summary?.delivered || 0} /></Card>
              </Col>
              <Col span={8}>
                <Card><Statistic title="Failed" value={selectedCampaign.summary?.failed || selectedCampaign.failed_count || 0} /></Card>
              </Col>
            </Row>

            <Divider />

            <Card size="small" title="Message">
              {selectedCampaign.subject ? <Paragraph><Text strong>{selectedCampaign.subject}</Text></Paragraph> : null}
              <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{selectedCampaign.content || 'No content override'}</Paragraph>
            </Card>

            <Divider />

            <Card size="small" title="Audience Snapshot">
              {selectedCampaign.target_segment ? (
                <Paragraph>
                  <Text strong>{selectedCampaign.target_segment.name}</Text>
                  <br />
                  <Text type="secondary">{selectedCampaign.target_segment.description || 'No segment description'}</Text>
                </Paragraph>
              ) : null}
              {(selectedCampaign.recipient_ids_snapshot || []).length > 0 ? (
                <Paragraph style={{ marginBottom: 0 }}>
                  Recipient IDs: {(selectedCampaign.recipient_ids_snapshot || []).join(', ')}
                </Paragraph>
              ) : (
                <Text type="secondary">Recipient snapshot will appear after queueing.</Text>
              )}
            </Card>

            <Divider />

            <Card size="small" title="Recent Notifications">
              {selectedCampaign.recent_notifications?.length ? (
                <List
                  dataSource={selectedCampaign.recent_notifications}
                  renderItem={(notification) => (
                    <List.Item>
                      <List.Item.Meta
                        title={
                          <Space>
                            <Text>{notification.user_name || `User ${notification.user_id}`}</Text>
                            <Tag>{notification.channel}</Tag>
                            <Tag color={notification.status === 'failed' ? 'error' : 'success'}>
                              {notification.status}
                            </Tag>
                          </Space>
                        }
                        description={
                          <div>
                            <div>{notification.message}</div>
                            <Text type="secondary">{formatDateTime(notification.created_at)}</Text>
                          </div>
                        }
                      />
                    </List.Item>
                  )}
                />
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No delivery records yet" />
              )}
            </Card>
          </>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="Select a campaign" />
        )}
      </Drawer>
      <Drawer
        title={selectedTemplate?.name || 'Template Details'}
        width={760}
        open={templateDrawerOpen}
        onClose={() => {
          setTemplateDrawerOpen(false);
          setSelectedTemplateId(null);
        }}
      >
        {templateDetailLoading && !selectedTemplate ? (
          <Card loading />
        ) : selectedTemplate ? (
          <>
            <Space style={{ marginBottom: 16 }} wrap>
              {renderChannelTag(selectedTemplate.channel, availableChannels)}
              <Tag>{selectedTemplate.notification_type?.replace(/_/g, ' ')}</Tag>
              <Tag color={selectedTemplate.is_active ? 'success' : 'default'}>
                {selectedTemplate.is_active ? 'ACTIVE' : 'INACTIVE'}
              </Tag>
              <Button icon={<EditOutlined />} onClick={() => handleEditTemplate(selectedTemplate)}>
                Edit
              </Button>
              <Button icon={<SendOutlined />} onClick={() => handleUseTemplate(selectedTemplate)}>
                Use in Campaign
              </Button>
              <Button
                icon={<ExperimentOutlined />}
                loading={templateTestSendMutation.isPending}
                onClick={() => {
                  try {
                    templateTestSendMutation.mutate({
                      templateId: selectedTemplate.id,
                      payload: {
                        variables: parseVariableJson(templatePreviewVariables)
                      }
                    });
                  } catch (error) {
                    message.error('Template variables must be valid JSON');
                  }
                }}
              >
                Send Test
              </Button>
              <Button
                icon={selectedTemplate.is_active ? <DeleteOutlined /> : <CheckCircleOutlined />}
                onClick={() => templateToggleMutation.mutate({
                  templateId: selectedTemplate.id,
                  isActive: !selectedTemplate.is_active
                })}
              >
                {selectedTemplate.is_active ? 'Deactivate' : 'Activate'}
              </Button>
            </Space>

            <Descriptions bordered column={2} size="small">
              <Descriptions.Item label="Usage">{selectedTemplate.usage_count} campaign(s)</Descriptions.Item>
              <Descriptions.Item label="Category">{selectedTemplate.category}</Descriptions.Item>
              <Descriptions.Item label="Created">{formatDateTime(selectedTemplate.created_at)}</Descriptions.Item>
              <Descriptions.Item label="Updated">{formatDateTime(selectedTemplate.updated_at || selectedTemplate.created_at)}</Descriptions.Item>
            </Descriptions>

            <Divider />

            <Card size="small" title="Template Source">
              {selectedTemplate.subject ? <Paragraph><Text strong>{selectedTemplate.subject}</Text></Paragraph> : null}
              <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{selectedTemplate.content}</Paragraph>
            </Card>

            <Divider />

            <Card
              size="small"
              title="Preview"
              extra={(
                <Button
                  size="small"
                  icon={<EyeOutlined />}
                  loading={templatePreviewMutation.isPending}
                  onClick={() => {
                    try {
                      runTemplatePreview(selectedTemplate.id);
                    } catch (error) {
                      message.error('Template variables must be valid JSON');
                    }
                  }}
                >
                  Refresh Preview
                </Button>
              )}
            >
              <Row gutter={16}>
                <Col span={8}>
                  <Select value={templatePreviewLanguage} style={{ width: '100%' }} onChange={setTemplatePreviewLanguage}>
                    <Option value="en">English</Option>
                    <Option value="ru">Russian</Option>
                    <Option value="uz">Uzbek</Option>
                  </Select>
                </Col>
                <Col span={16}>
                  <Input.TextArea
                    rows={6}
                    value={templatePreviewVariables}
                    onChange={(event) => setTemplatePreviewVariables(event.target.value)}
                    placeholder='{"user_name":"Admin User"}'
                  />
                </Col>
              </Row>

              <Divider />

              {templatePreview ? (
                <>
                  {templatePreview.subject ? <Paragraph><Text strong>{templatePreview.subject}</Text></Paragraph> : null}
                  <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{templatePreview.content}</Paragraph>
                </>
              ) : (
                <Text type="secondary">Generate a preview to inspect rendered content.</Text>
              )}
            </Card>
          </>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="Select a template" />
        )}
      </Drawer>
    </div>
  );
};

export default Notifications;
