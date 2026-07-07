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
import { useTranslation } from 'react-i18next';
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
  const { t } = useTranslation('notifications');
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
      message.error(error?.response?.data?.message || t('toast_preview_failed', { defaultValue: 'Failed to render template preview' }));
    },
  });

  const templateTestSendMutation = useMutation({
    mutationFn: ({ templateId, payload }) => adminService.testSendNotificationTemplate(templateId, payload),

    onSuccess: () => {
      message.success(t('toast_test_sent', { defaultValue: 'Template test notification sent' }));
    },

    onError: (error) => {
      message.error(error?.response?.data?.message || t('toast_test_failed', { defaultValue: 'Failed to send test notification' }));
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
          throw new Error(t('error_schedule_time_required', { defaultValue: 'Schedule time is required' }));
        }
        campaign = await adminService.sendNotificationCampaign(campaign.id, { send_now: false });
      }

      return campaign;
    },

    onSuccess: (_, variables) => {
      message.success(
        variables.mode === 'draft'
          ? t('toast_campaign_saved', { defaultValue: 'Campaign saved' })
          : variables.mode === 'send_now'
            ? t('toast_campaign_sent', { defaultValue: 'Campaign sent' })
            : t('toast_campaign_scheduled', { defaultValue: 'Campaign scheduled' })
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
      message.error(error?.response?.data?.message || error?.message || t('toast_campaign_save_failed', { defaultValue: 'Failed to save campaign' }));
    },
  });

  const campaignDeleteMutation = useMutation({
    mutationFn: (campaignId) => adminService.deleteNotificationCampaign(campaignId),

    onSuccess: () => {
      message.success(t('toast_campaign_deleted', { defaultValue: 'Campaign deleted' }));
      queryClient.invalidateQueries({
        queryKey: ['notification-campaigns'],
      });
      setCampaignDrawerOpen(false);
      setSelectedCampaignId(null);
    },

    onError: (error) => {
      message.error(error?.response?.data?.message || t('toast_campaign_delete_failed', { defaultValue: 'Failed to delete campaign' }));
    },
  });

  const campaignDuplicateMutation = useMutation({
    mutationFn: (campaignId) => adminService.duplicateNotificationCampaign(campaignId),

    onSuccess: () => {
      message.success(t('toast_campaign_duplicated', { defaultValue: 'Campaign duplicated' }));
      queryClient.invalidateQueries({
        queryKey: ['notification-campaigns'],
      });
    },

    onError: (error) => {
      message.error(error?.response?.data?.message || t('toast_campaign_duplicate_failed', { defaultValue: 'Failed to duplicate campaign' }));
    },
  });

  const campaignCancelMutation = useMutation({
    mutationFn: (campaignId) => adminService.cancelNotificationCampaign(campaignId),

    onSuccess: (_, campaignId) => {
      message.success(t('toast_campaign_cancelled', { defaultValue: 'Campaign cancelled' }));
      queryClient.invalidateQueries({
        queryKey: ['notification-campaigns'],
      });
      queryClient.invalidateQueries({
        queryKey: ['notification-campaign-detail', campaignId],
      });
    },

    onError: (error) => {
      message.error(error?.response?.data?.message || t('toast_campaign_cancel_failed', { defaultValue: 'Failed to cancel campaign' }));
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
      message.success(editingTemplateId ? t('toast_template_updated', { defaultValue: 'Template updated' }) : t('toast_template_created', { defaultValue: 'Template created' }));
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
      message.error(error?.response?.data?.message || t('toast_template_save_failed', { defaultValue: 'Failed to save template' }));
    },
  });

  const templateToggleMutation = useMutation({
    mutationFn: ({ templateId, isActive }) => adminService.updateNotificationTemplate(templateId, { is_active: isActive }),

    onSuccess: () => {
      message.success(t('toast_template_status_updated', { defaultValue: 'Template status updated' }));
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
      message.error(error?.response?.data?.message || t('toast_template_status_failed', { defaultValue: 'Failed to update template status' }));
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
      message.error(result.message || t('toast_export_failed', { defaultValue: 'Export failed' }));
      return;
    }
    message.success(t('toast_export_generated', { defaultValue: 'Campaign export generated' }));
  };

  const campaignColumns = [
    {
      title: t('col_campaign', { defaultValue: 'Campaign' }),
      dataIndex: 'name',
      key: 'name',
      render: (_, record) => (
        <div>
          <div style={{ fontWeight: 600 }}>{record.name}</div>
          <Text type="secondary">{record.subject || record.content || t('no_custom_content', { defaultValue: 'No custom content' })}</Text>
        </div>
      )
    },
    {
      title: t('col_type', { defaultValue: 'Type' }),
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
      title: t('col_channel', { defaultValue: 'Channel' }),
      dataIndex: 'channel',
      key: 'channel',
      render: (value) => renderChannelTag(value, availableChannels)
    },
    {
      title: t('col_recipients', { defaultValue: 'Recipients' }),
      dataIndex: 'recipient_count',
      key: 'recipient_count',
      render: (count) => <Badge count={count} style={{ backgroundColor: '#1677ff' }} />
    },
    {
      title: t('col_delivery', { defaultValue: 'Delivery' }),
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
      title: t('status', { defaultValue: 'Status' }),
      dataIndex: 'status',
      key: 'status',
      // eslint-disable-next-line security/detect-object-injection
      render: (status) => <Tag color={CAMPAIGN_STATUS_COLORS[status]}>{status?.toUpperCase()}</Tag>
    },
    {
      title: t('col_schedule', { defaultValue: 'Schedule' }),
      dataIndex: 'scheduled_at',
      key: 'scheduled_at',
      render: (value) => value ? formatDateTime(value) : t('immediate', { defaultValue: 'Immediate' })
    },
    {
      title: t('actions', { defaultValue: 'Actions' }),
      key: 'actions',
      render: (_, record) => (
        <Dropdown
          trigger={['click']}
          menu={{
            items: [
              { key: 'view', label: t('view', { defaultValue: 'View' }), icon: <EyeOutlined />, onClick: () => handleViewCampaign(record) },
              {
                key: 'edit',
                label: t('edit', { defaultValue: 'Edit' }),
                icon: <EditOutlined />,
                disabled: !['draft', 'scheduled'].includes(record.status),
                onClick: () => handleEditCampaign(record)
              },
              { key: 'duplicate', label: t('duplicate', { defaultValue: 'Duplicate' }), icon: <CopyOutlined />, onClick: () => handleDuplicateCampaign(record) },
              {
                key: 'cancel',
                label: t('cancel', { defaultValue: 'Cancel' }),
                icon: <StopOutlined />,
                disabled: !['scheduled', 'sending'].includes(record.status),
                onClick: () => campaignCancelMutation.mutate(record.id)
              },
              {
                key: 'delete',
                label: t('delete', { defaultValue: 'Delete' }),
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
      title: t('col_template', { defaultValue: 'Template' }),
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
      title: t('col_type', { defaultValue: 'Type' }),
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
      title: t('col_channel', { defaultValue: 'Channel' }),
      dataIndex: 'channel',
      key: 'channel',
      render: (value) => renderChannelTag(value, availableChannels)
    },
    {
      title: t('col_usage', { defaultValue: 'Usage' }),
      dataIndex: 'usage_count',
      key: 'usage_count',
      render: (value) => t('usage_count', { count: value, defaultValue: '{{count}} campaign(s)' })
    },
    {
      title: t('status', { defaultValue: 'Status' }),
      dataIndex: 'is_active',
      key: 'is_active',
      render: (value) => <Tag color={value ? 'success' : 'default'}>{value ? t('badge_active', { defaultValue: 'ACTIVE' }) : t('badge_inactive', { defaultValue: 'INACTIVE' })}</Tag>
    },
    {
      title: t('col_updated', { defaultValue: 'Updated' }),
      dataIndex: 'updated_at',
      key: 'updated_at',
      render: (value, record) => formatDate(value || record.created_at)
    },
    {
      title: t('actions', { defaultValue: 'Actions' }),
      key: 'actions',
      render: (_, record) => (
        <Dropdown
          trigger={['click']}
          menu={{
            items: [
              { key: 'view', label: t('view', { defaultValue: 'View' }), icon: <EyeOutlined />, onClick: () => handleViewTemplate(record) },
              { key: 'edit', label: t('edit', { defaultValue: 'Edit' }), icon: <EditOutlined />, onClick: () => handleEditTemplate(record) },
              { key: 'use', label: t('use_in_campaign', { defaultValue: 'Use in Campaign' }), icon: <SendOutlined />, onClick: () => handleUseTemplate(record) },
              {
                key: 'toggle',
                label: record.is_active ? t('deactivate', { defaultValue: 'Deactivate' }) : t('activate', { defaultValue: 'Activate' }),
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
            label: t('tab_campaigns', { defaultValue: 'Campaigns' }),
            children: (
              <>
                <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
                  <Col xs={24} sm={12} lg={6}>
                    <Card><Statistic title={t('stat_total_campaigns', { defaultValue: 'Total Campaigns' })} value={totalCampaigns} prefix={<BellOutlined />} /></Card>
                  </Col>
                  <Col xs={24} sm={12} lg={6}>
                    <Card><Statistic title={t('stat_active_campaigns', { defaultValue: 'Active Campaigns' })} value={activeCampaigns} prefix={<ClockCircleOutlined />} /></Card>
                  </Col>
                  <Col xs={24} sm={12} lg={6}>
                    <Card><Statistic title={t('stat_messages_sent', { defaultValue: 'Messages Sent' })} value={totalSent} prefix={<CheckCircleOutlined />} /></Card>
                  </Col>
                  <Col xs={24} sm={12} lg={6}>
                    <Card><Statistic title={t('stat_delivery_rate', { defaultValue: 'Delivery Rate' })} value={deliveryRate} precision={1} suffix="%" /></Card>
                  </Col>
                </Row>

                <Card>
                  <div className="table-actions">
                    <Space wrap>
                      <Input.Search
                        allowClear
                        placeholder={t('search_campaigns_placeholder', { defaultValue: 'Search campaigns' })}
                        prefix={<SearchOutlined />}
                        style={{ width: 260 }}
                        onSearch={(value) => {
                          setCampaignPagination((prev) => ({ ...prev, page: 1 }));
                          setCampaignFilters((prev) => ({ ...prev, search: value }));
                        }}
                      />
                      <Select
                        allowClear
                        placeholder={t('status', { defaultValue: 'Status' })}
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
                        placeholder={t('channel', { defaultValue: 'Channel' })}
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
                        placeholder={t('audience', { defaultValue: 'Audience' })}
                        style={{ width: 180 }}
                        value={campaignFilters.target_audience}
                        onChange={(value) => {
                          setCampaignPagination((prev) => ({ ...prev, page: 1 }));
                          setCampaignFilters((prev) => ({ ...prev, target_audience: value }));
                        }}
                      >
                        {AUDIENCE_OPTIONS.map((audience) => (
                          <Option key={audience.value} value={audience.value}>{t(`audience_${audience.value}`, { defaultValue: audience.label })}</Option>
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
                        {t('create_campaign', { defaultValue: 'Create Campaign' })}
                      </Button>
                      <Button icon={<ExportOutlined />} onClick={handleExportCampaigns}>
                        {t('export', { defaultValue: 'Export' })}
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
            label: t('tab_templates', { defaultValue: 'Templates' }),
            children: (
              <>
                <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
                  <Col xs={24} sm={8}>
                    <Card><Statistic title={t('stat_total_templates', { defaultValue: 'Total Templates' })} value={templateCollection?.pagination?.total || 0} prefix={<MailOutlined />} /></Card>
                  </Col>
                  <Col xs={24} sm={8}>
                    <Card><Statistic title={t('stat_telegram_templates', { defaultValue: 'Telegram Templates' })} value={templates.filter((item) => item.channel === 'telegram').length} prefix={<SendOutlined />} /></Card>
                  </Col>
                  <Col xs={24} sm={8}>
                    <Card><Statistic title={t('stat_inactive_templates', { defaultValue: 'Inactive Templates' })} value={templates.filter((item) => item.is_active === false).length} prefix={<ClockCircleOutlined />} /></Card>
                  </Col>
                </Row>

                <Card>
                  <div className="table-actions">
                    <Space wrap>
                      <Input.Search
                        allowClear
                        placeholder={t('search_templates_placeholder', { defaultValue: 'Search templates' })}
                        style={{ width: 260 }}
                        onSearch={(value) => {
                          setTemplatePagination((prev) => ({ ...prev, page: 1 }));
                          setTemplateFilters((prev) => ({ ...prev, search: value }));
                        }}
                      />
                      <Select
                        allowClear
                        placeholder={t('channel', { defaultValue: 'Channel' })}
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
                        placeholder={t('notification_type', { defaultValue: 'Notification Type' })}
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
                        placeholder={t('status', { defaultValue: 'Status' })}
                        style={{ width: 140 }}
                        value={templateFilters.is_active}
                        onChange={(value) => {
                          setTemplatePagination((prev) => ({ ...prev, page: 1 }));
                          setTemplateFilters((prev) => ({ ...prev, is_active: value }));
                        }}
                      >
                        <Option value={true}>{t('active', { defaultValue: 'Active' })}</Option>
                        <Option value={false}>{t('inactive', { defaultValue: 'Inactive' })}</Option>
                      </Select>
                    </Space>

                    <Button type="primary" icon={<PlusOutlined />} onClick={handleCreateTemplate}>
                      {t('create_template', { defaultValue: 'Create Template' })}
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
        title={campaignModalMode === 'edit' ? t('edit_campaign_title', { defaultValue: 'Edit Campaign' }) : t('create_campaign', { defaultValue: 'Create Campaign' })}
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
              <Form.Item name="name" label={t('campaign_name', { defaultValue: 'Campaign Name' })} rules={[{ required: true, message: t('campaign_name_required', { defaultValue: 'Campaign name is required' }) }]}>
                <Input placeholder={t('campaign_name_placeholder', { defaultValue: 'Retention push for dormant customers' })} />
              </Form.Item>
            </Col>
            <Col span={10}>
              <Form.Item name="template_id" label={t('template_label', { defaultValue: 'Template' })}>
                <Select allowClear placeholder={t('optional_template_placeholder', { defaultValue: 'Optional saved template' })}>
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
              <Form.Item name="notification_type" label={t('notification_type', { defaultValue: 'Notification Type' })} rules={[{ required: true, message: t('select_notification_type_required', { defaultValue: 'Select a notification type' }) }]}>
                <Select showSearch optionFilterProp="children" placeholder={t('select_notification_type', { defaultValue: 'Select notification type' })}>
                  {notificationTypes.map((type) => (
                    <Option key={type.value} value={type.value}>
                      {type.label}
                    </Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="channel" label={t('channel', { defaultValue: 'Channel' })} rules={[{ required: true, message: t('select_channel_required', { defaultValue: 'Select a channel' }) }]}>
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
                  label={t('subject_title_label', { defaultValue: 'Subject / Title' })}
                  rules={requiresSubject ? [{ required: true, message: t('subject_required_email', { defaultValue: 'Subject is required for email campaigns without a template' }) }] : []}
                >
                  <Input placeholder={t('optional_override_placeholder', { defaultValue: 'Optional override' })} />
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
                  label={t('message_content_label', { defaultValue: 'Message Content' })}
                  rules={!templateSelected ? [{ required: true, message: t('content_required', { defaultValue: 'Message content is required when no template is selected' }) }] : []}
                >
                  <Input.TextArea rows={6} placeholder={t('placeholders_hint', { defaultValue: 'Use {user_name}, {order_number}, {company_name} and other placeholders' })} />
                </Form.Item>
              );
            }}
          </Form.Item>

          <Row gutter={16}>
            <Col span={8}>
              <Form.Item name="target_audience" label={t('target_audience_label', { defaultValue: 'Target Audience' })} rules={[{ required: true, message: t('select_audience_required', { defaultValue: 'Select an audience' }) }]}>
                <Select placeholder={t('audience', { defaultValue: 'Audience' })}>
                  {AUDIENCE_OPTIONS.map((option) => (
                    <Option key={option.value} value={option.value}>{t(`audience_${option.value}`, { defaultValue: option.label })}</Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="priority" label={t('priority_label', { defaultValue: 'Priority' })}>
                <Select placeholder={t('priority_label', { defaultValue: 'Priority' })}>
                  {PRIORITY_OPTIONS.map((option) => (
                    <Option key={option.value} value={option.value}>{t(`priority_${option.value}`, { defaultValue: option.label })}</Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="scheduled_at" label={t('schedule_label', { defaultValue: 'Schedule' })}>
                <DatePicker showTime style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          {campaignAudience === 'custom_segment' && (
            <Row gutter={16}>
              <Col span={12}>
                <Form.Item name="target_segment_id" label={t('user_segment_label', { defaultValue: 'User Segment' })}>
                  <Select allowClear placeholder={t('optional_segment_placeholder', { defaultValue: 'Optional saved segment' })}>
                    {segmentOptions.map((segment) => (
                      <Option key={segment.id} value={segment.id}>
                        {segment.name} ({segment.user_count || 0})
                      </Option>
                    ))}
                  </Select>
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item name="specific_user_ids" label={t('specific_user_ids_label', { defaultValue: 'Specific User IDs' })}>
                  <Select
                    mode="tags"
                    tokenSeparators={[',', ' ']}
                    placeholder={t('user_ids_placeholder', { defaultValue: 'Enter user IDs if needed' })}
                    open={false}
                  />
                </Form.Item>
              </Col>
            </Row>
          )}

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setCampaignModalOpen(false)}>{t('close', { defaultValue: 'Close' })}</Button>
              <Button
                icon={<SaveOutlined />}
                loading={campaignSaveMutation.isPending && campaignSubmitMode === 'draft'}
                onClick={() => {
                  setCampaignSubmitMode('draft');
                  campaignForm.submit();
                }}
              >
                {t('save_draft', { defaultValue: 'Save Draft' })}
              </Button>
              <Button
                icon={<SendOutlined />}
                loading={campaignSaveMutation.isPending && campaignSubmitMode === 'send_now'}
                onClick={() => {
                  setCampaignSubmitMode('send_now');
                  campaignForm.submit();
                }}
              >
                {t('send_now', { defaultValue: 'Send Now' })}
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
                {t('schedule_label', { defaultValue: 'Schedule' })}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title={templateModalMode === 'edit' ? t('edit_template_title', { defaultValue: 'Edit Template' }) : t('create_template', { defaultValue: 'Create Template' })}
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
              <Form.Item name="name" label={t('template_name_label', { defaultValue: 'Template Name' })} rules={[{ required: true, message: t('template_name_required', { defaultValue: 'Template name is required' }) }]}>
                <Input placeholder={t('template_name_placeholder', { defaultValue: 'Telegram delivery reminder' })} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="notification_type" label={t('notification_type', { defaultValue: 'Notification Type' })} rules={[{ required: true, message: t('select_notification_type_required', { defaultValue: 'Select a notification type' }) }]}>
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
              <Form.Item name="channel" label={t('channel', { defaultValue: 'Channel' })} rules={[{ required: true, message: t('select_channel_required', { defaultValue: 'Select a channel' }) }]}>
                <Select>
                  {creatableChannels.map((channel) => (
                    <Option key={channel.value} value={channel.value}>{channel.label}</Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="is_active" label={t('active', { defaultValue: 'Active' })} valuePropName="checked">
                <Switch />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item
            name="subject"
            label={t('subject_title_label', { defaultValue: 'Subject / Title' })}
            rules={templateChannel === 'email' ? [{ required: true, message: t('subject_required_email_template', { defaultValue: 'Subject is required for email templates' }) }] : []}
          >
            <Input placeholder={t('optional_non_email_placeholder', { defaultValue: 'Optional for non-email channels' })} />
          </Form.Item>

          <Form.Item name="content" label={t('template_content_label', { defaultValue: 'Template Content' })} rules={[{ required: true, message: t('template_content_required', { defaultValue: 'Template content is required' }) }]}>
            <Input.TextArea rows={8} placeholder={t('placeholders_hint', { defaultValue: 'Use {user_name}, {order_number}, {company_name} and other placeholders' })} />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setTemplateModalOpen(false)}>{t('close', { defaultValue: 'Close' })}</Button>
              <Button type="primary" loading={templateSaveMutation.isPending} htmlType="submit">
                {templateModalMode === 'edit' ? t('update_template_button', { defaultValue: 'Update Template' }) : t('create_template', { defaultValue: 'Create Template' })}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
      <Drawer
        title={selectedCampaign?.name || t('campaign_details_fallback', { defaultValue: 'Campaign Details' })}
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
                {t('edit', { defaultValue: 'Edit' })}
              </Button>
              <Button icon={<CopyOutlined />} onClick={() => handleDuplicateCampaign(selectedCampaign)}>
                {t('duplicate', { defaultValue: 'Duplicate' })}
              </Button>
              <Button
                icon={<SendOutlined />}
                disabled={!['draft'].includes(selectedCampaign.status)}
                onClick={() => adminService.sendNotificationCampaign(selectedCampaign.id, { send_now: true }).then(() => {
                  message.success(t('toast_campaign_queued', { defaultValue: 'Campaign queued' }));
                  queryClient.invalidateQueries({
                    queryKey: ['notification-campaigns'],
                  });
                  queryClient.invalidateQueries({
                    queryKey: ['notification-campaign-detail', selectedCampaign.id],
                  });
                }).catch((error) => {
                  message.error(error?.response?.data?.message || t('toast_queue_failed', { defaultValue: 'Failed to queue campaign' }));
                })}
              >
                {t('send_now', { defaultValue: 'Send Now' })}
              </Button>
              <Button
                icon={<StopOutlined />}
                disabled={!['scheduled', 'sending'].includes(selectedCampaign.status)}
                onClick={() => campaignCancelMutation.mutate(selectedCampaign.id)}
              >
                {t('cancel', { defaultValue: 'Cancel' })}
              </Button>
            </Space>

            <Descriptions bordered column={2} size="small">
              <Descriptions.Item label={t('audience', { defaultValue: 'Audience' })}>{selectedCampaign.target_audience}</Descriptions.Item>
              <Descriptions.Item label={t('priority_label', { defaultValue: 'Priority' })}>{selectedCampaign.priority}</Descriptions.Item>
              <Descriptions.Item label={t('scheduled_label', { defaultValue: 'Scheduled' })}>{selectedCampaign.scheduled_at ? formatDateTime(selectedCampaign.scheduled_at) : t('immediate', { defaultValue: 'Immediate' })}</Descriptions.Item>
              <Descriptions.Item label={t('queued_label', { defaultValue: 'Queued' })}>{selectedCampaign.queued_at ? formatDateTime(selectedCampaign.queued_at) : t('not_queued', { defaultValue: 'Not queued' })}</Descriptions.Item>
              <Descriptions.Item label={t('started_label', { defaultValue: 'Started' })}>{selectedCampaign.started_at ? formatDateTime(selectedCampaign.started_at) : t('not_started', { defaultValue: 'Not started' })}</Descriptions.Item>
              <Descriptions.Item label={t('completed_label', { defaultValue: 'Completed' })}>{selectedCampaign.completed_at ? formatDateTime(selectedCampaign.completed_at) : t('not_completed', { defaultValue: 'Not completed' })}</Descriptions.Item>
              <Descriptions.Item label={t('template_label', { defaultValue: 'Template' })}>{selectedCampaign.template?.name || t('custom_content', { defaultValue: 'Custom content' })}</Descriptions.Item>
              <Descriptions.Item label={t('col_recipients', { defaultValue: 'Recipients' })}>{selectedCampaign.recipient_count}</Descriptions.Item>
              <Descriptions.Item label={t('created_label', { defaultValue: 'Created' })}>{formatDateTime(selectedCampaign.created_at)}</Descriptions.Item>
              <Descriptions.Item label={t('updated_label', { defaultValue: 'Updated' })}>{formatDateTime(selectedCampaign.updated_at)}</Descriptions.Item>
            </Descriptions>

            <Divider />

            <Row gutter={[16, 16]}>
              <Col span={8}>
                <Card><Statistic title={t('sent_label', { defaultValue: 'Sent' })} value={selectedCampaign.summary?.sent || selectedCampaign.sent_count || 0} /></Card>
              </Col>
              <Col span={8}>
                <Card><Statistic title={t('delivered_label', { defaultValue: 'Delivered' })} value={selectedCampaign.summary?.delivered || 0} /></Card>
              </Col>
              <Col span={8}>
                <Card><Statistic title={t('failed_label', { defaultValue: 'Failed' })} value={selectedCampaign.summary?.failed || selectedCampaign.failed_count || 0} /></Card>
              </Col>
            </Row>

            <Divider />

            <Card size="small" title={t('message_card_title', { defaultValue: 'Message' })}>
              {selectedCampaign.subject ? <Paragraph><Text strong>{selectedCampaign.subject}</Text></Paragraph> : null}
              <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{selectedCampaign.content || t('no_content_override', { defaultValue: 'No content override' })}</Paragraph>
            </Card>

            <Divider />

            <Card size="small" title={t('audience_snapshot_title', { defaultValue: 'Audience Snapshot' })}>
              {selectedCampaign.target_segment ? (
                <Paragraph>
                  <Text strong>{selectedCampaign.target_segment.name}</Text>
                  <br />
                  <Text type="secondary">{selectedCampaign.target_segment.description || t('no_segment_description', { defaultValue: 'No segment description' })}</Text>
                </Paragraph>
              ) : null}
              {(selectedCampaign.recipient_ids_snapshot || []).length > 0 ? (
                <Paragraph style={{ marginBottom: 0 }}>
                  {t('recipient_ids_prefix', {
                    ids: (selectedCampaign.recipient_ids_snapshot || []).join(', '),
                    defaultValue: 'Recipient IDs: {{ids}}',
                  })}
                </Paragraph>
              ) : (
                <Text type="secondary">{t('recipient_snapshot_pending', { defaultValue: 'Recipient snapshot will appear after queueing.' })}</Text>
              )}
            </Card>

            <Divider />

            <Card size="small" title={t('recent_notifications_title', { defaultValue: 'Recent Notifications' })}>
              {selectedCampaign.recent_notifications?.length ? (
                <List
                  dataSource={selectedCampaign.recent_notifications}
                  renderItem={(notification) => (
                    <List.Item>
                      <List.Item.Meta
                        title={
                          <Space>
                            <Text>{notification.user_name || t('user_fallback_with_id', { id: notification.user_id, defaultValue: 'User {{id}}' })}</Text>
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
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('no_delivery_records', { defaultValue: 'No delivery records yet' })} />
              )}
            </Card>
          </>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('select_a_campaign', { defaultValue: 'Select a campaign' })} />
        )}
      </Drawer>
      <Drawer
        title={selectedTemplate?.name || t('template_details_fallback', { defaultValue: 'Template Details' })}
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
                {selectedTemplate.is_active ? t('badge_active', { defaultValue: 'ACTIVE' }) : t('badge_inactive', { defaultValue: 'INACTIVE' })}
              </Tag>
              <Button icon={<EditOutlined />} onClick={() => handleEditTemplate(selectedTemplate)}>
                {t('edit', { defaultValue: 'Edit' })}
              </Button>
              <Button icon={<SendOutlined />} onClick={() => handleUseTemplate(selectedTemplate)}>
                {t('use_in_campaign', { defaultValue: 'Use in Campaign' })}
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
                    message.error(t('toast_invalid_variables_json', { defaultValue: 'Template variables must be valid JSON' }));
                  }
                }}
              >
                {t('send_test', { defaultValue: 'Send Test' })}
              </Button>
              <Button
                icon={selectedTemplate.is_active ? <DeleteOutlined /> : <CheckCircleOutlined />}
                onClick={() => templateToggleMutation.mutate({
                  templateId: selectedTemplate.id,
                  isActive: !selectedTemplate.is_active
                })}
              >
                {selectedTemplate.is_active ? t('deactivate', { defaultValue: 'Deactivate' }) : t('activate', { defaultValue: 'Activate' })}
              </Button>
            </Space>

            <Descriptions bordered column={2} size="small">
              <Descriptions.Item label={t('usage_label', { defaultValue: 'Usage' })}>{t('usage_count', { count: selectedTemplate.usage_count, defaultValue: '{{count}} campaign(s)' })}</Descriptions.Item>
              <Descriptions.Item label={t('category_label', { defaultValue: 'Category' })}>{selectedTemplate.category}</Descriptions.Item>
              <Descriptions.Item label={t('created_label', { defaultValue: 'Created' })}>{formatDateTime(selectedTemplate.created_at)}</Descriptions.Item>
              <Descriptions.Item label={t('updated_label', { defaultValue: 'Updated' })}>{formatDateTime(selectedTemplate.updated_at || selectedTemplate.created_at)}</Descriptions.Item>
            </Descriptions>

            <Divider />

            <Card size="small" title={t('template_source_title', { defaultValue: 'Template Source' })}>
              {selectedTemplate.subject ? <Paragraph><Text strong>{selectedTemplate.subject}</Text></Paragraph> : null}
              <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{selectedTemplate.content}</Paragraph>
            </Card>

            <Divider />

            <Card
              size="small"
              title={t('preview_title', { defaultValue: 'Preview' })}
              extra={(
                <Button
                  size="small"
                  icon={<EyeOutlined />}
                  loading={templatePreviewMutation.isPending}
                  onClick={() => {
                    try {
                      runTemplatePreview(selectedTemplate.id);
                    } catch (error) {
                      message.error(t('toast_invalid_variables_json', { defaultValue: 'Template variables must be valid JSON' }));
                    }
                  }}
                >
                  {t('refresh_preview_button', { defaultValue: 'Refresh Preview' })}
                </Button>
              )}
            >
              <Row gutter={16}>
                <Col span={8}>
                  <Select value={templatePreviewLanguage} style={{ width: '100%' }} onChange={setTemplatePreviewLanguage}>
                    <Option value="en">{t('lang_english', { defaultValue: 'English' })}</Option>
                    <Option value="ru">{t('lang_russian', { defaultValue: 'Russian' })}</Option>
                    <Option value="uz">{t('lang_uzbek', { defaultValue: 'Uzbek' })}</Option>
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
                <Text type="secondary">{t('generate_preview_hint', { defaultValue: 'Generate a preview to inspect rendered content.' })}</Text>
              )}
            </Card>
          </>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('select_a_template', { defaultValue: 'Select a template' })} />
        )}
      </Drawer>
    </div>
  );
};

export default Notifications;
