import React, { useState, useEffect } from 'react';
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
  Row,
  Col,
  Statistic,
  message,
  Divider,
  Progress,
  Upload,
  Tabs,
  Typography,
  Popconfirm,
  Tooltip,
  Alert,
  Switch
} from 'antd';
import {
  SearchOutlined,
  TranslationOutlined,
  MoreOutlined,
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  EyeOutlined,
  ExportOutlined,
  ImportOutlined,
  SyncOutlined,
  PercentageOutlined,
  CheckCircleOutlined,
  ExclamationCircleOutlined,
  DownloadOutlined,
  UploadOutlined,
  GlobalOutlined
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient } from 'react-query';
import adminService from '../services/adminService';

const { Option } = Select;
const { TextArea } = Input;
const { Title, Text, Paragraph } = Typography;
const { TabPane } = Tabs;

const LANGUAGES = {
  'en': { name: 'English', flag: '🇺🇸', color: 'blue' },
  'uz': { name: 'O\'zbek', flag: '🇺🇿', color: 'green' },
  'ru': { name: 'Русский', flag: '🇷🇺', color: 'red' }
};

const Translations = () => {
  // State management
  const [searchText, setSearchText] = useState('');
  const [categoryFilter, setCategoryFilter] = useState('');
  const [languageFilter, setLanguageFilter] = useState('');
  const [selectedTranslation, setSelectedTranslation] = useState(null);
  const [isEditModalVisible, setIsEditModalVisible] = useState(false);
  const [isCreateModalVisible, setIsCreateModalVisible] = useState(false);
  const [isSyncModalVisible, setIsSyncModalVisible] = useState(false);
  const [isImportModalVisible, setIsImportModalVisible] = useState(false);
  const [activeTab, setActiveTab] = useState('translations');
  const [pagination, setPagination] = useState({ current: 1, pageSize: 50 });

  const [form] = Form.useForm();
  const [syncForm] = Form.useForm();
  const [importForm] = Form.useForm();
  const queryClient = useQueryClient();

  // Fetch translations
  const { data: translationsData, isLoading: translationsLoading, refetch: refetchTranslations } = useQuery(
    ['translations', searchText, categoryFilter, languageFilter, pagination],
    () => adminService.getTranslations({
      page: pagination.current,
      per_page: pagination.pageSize,
      search: searchText || undefined,
      category: categoryFilter || undefined,
      language: languageFilter || undefined
    }),
    { keepPreviousData: true }
  );

  // Fetch completion stats
  const { data: completionData, isLoading: completionLoading } = useQuery(
    ['translations-completion', categoryFilter],
    () => adminService.getTranslationCompletion({ category: categoryFilter || undefined }),
    { refetchInterval: 3600000 } // Refresh every 1 hour
  );

  // Fetch missing translations
  const { data: missingData, isLoading: missingLoading } = useQuery(
    ['translations-missing', categoryFilter, languageFilter],
    () => adminService.getMissingTranslations({
      category: categoryFilter || undefined,
      language: languageFilter || undefined
    })
  );

  // Available categories
  const CATEGORIES = ['telegram', 'ui', 'email', 'sms', 'common'];

  // Mutations
  const createTranslationMutation = useMutation(adminService.createTranslation, {
    onSuccess: () => {
      message.success('Translation created successfully');
      setIsCreateModalVisible(false);
      form.resetFields();
      queryClient.invalidateQueries('translations');
      queryClient.invalidateQueries('translations-completion');
      queryClient.invalidateQueries('translations-missing');
    },
    onError: (error) => {
      message.error(error.message || 'Failed to create translation');
    }
  });

  const updateTranslationMutation = useMutation(adminService.updateTranslation, {
    onSuccess: () => {
      message.success('Translation updated successfully');
      setIsEditModalVisible(false);
      form.resetFields();
      queryClient.invalidateQueries('translations');
      queryClient.invalidateQueries('translations-completion');
    },
    onError: (error) => {
      message.error(error.message || 'Failed to update translation');
    }
  });

  const deleteTranslationMutation = useMutation(adminService.deleteTranslation, {
    onSuccess: () => {
      message.success('Translation deleted successfully');
      queryClient.invalidateQueries('translations');
      queryClient.invalidateQueries('translations-completion');
      queryClient.invalidateQueries('translations-missing');
    },
    onError: (error) => {
      message.error(error.message || 'Failed to delete translation');
    }
  });

  const syncTranslationsMutation = useMutation(adminService.syncEntityTranslations, {
    onSuccess: (data) => {
      message.success(data.message || 'Translations synced successfully');
      setIsSyncModalVisible(false);
      syncForm.resetFields();
      queryClient.invalidateQueries('translations');
      queryClient.invalidateQueries('translations-completion');
    },
    onError: (error) => {
      message.error(error.message || 'Failed to sync translations');
    }
  });

  const importTranslationsMutation = useMutation(adminService.importTranslations, {
    onSuccess: (data) => {
      message.success(`Import completed: ${data.results.created} created, ${data.results.updated} updated`);
      setIsImportModalVisible(false);
      importForm.resetFields();
      queryClient.invalidateQueries('translations');
      queryClient.invalidateQueries('translations-completion');
    },
    onError: (error) => {
      message.error(error.message || 'Failed to import translations');
    }
  });

  // Table columns for translations
  const translationColumns = [
    {
      title: 'Category',
      dataIndex: 'category',
      key: 'category',
      width: 120,
      render: (text) => <Tag color="cyan">{text}</Tag>,
    },
    {
      title: 'Key',
      dataIndex: 'key',
      key: 'key',
      ellipsis: { showTitle: false },
      render: (text) => (
        <Tooltip placement="topLeft" title={text}>
          <Text code>{text}</Text>
        </Tooltip>
      ),
    },
    {
      title: 'Language',
      dataIndex: 'language',
      key: 'language',
      width: 100,
      render: (lang) => {
        const langInfo = LANGUAGES[lang] || { name: lang, flag: '🌐', color: 'default' };
        return (
          <Space>
            <span>{langInfo.flag}</span>
            <Tag color={langInfo.color}>{langInfo.name}</Tag>
          </Space>
        );
      },
    },
    {
      title: 'Value',
      dataIndex: 'value',
      key: 'value',
      ellipsis: { showTitle: false },
      render: (value) => (
        <Tooltip placement="topLeft" title={value}>
          {value && value.length > 100 ? `${value.substring(0, 100)}...` : value}
        </Tooltip>
      ),
    },
    {
      title: 'Status',
      key: 'status',
      width: 80,
      render: (_, record) => (
        <Tag color={record.is_active ? 'success' : 'default'}>
          {record.is_active ? 'Active' : 'Inactive'}
        </Tag>
      ),
    },
    {
      title: 'Actions',
      key: 'actions',
      width: 120,
      render: (_, record) => (
        <Space size="small">
          <Tooltip title="Edit">
            <Button
              type="text"
              icon={<EditOutlined />}
              onClick={() => handleEdit(record)}
              size="small"
            />
          </Tooltip>
          <Popconfirm
            title="Are you sure you want to delete this translation?"
            onConfirm={() => deleteTranslationMutation.mutate(record.id)}
            okText="Yes"
            cancelText="No"
          >
            <Tooltip title="Delete">
              <Button
                type="text"
                danger
                icon={<DeleteOutlined />}
                size="small"
              />
            </Tooltip>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  // Missing translations columns
  const missingColumns = [
    {
      title: 'Entity',
      key: 'entity',
      render: (_, record) => (
        <Space direction="vertical" size={0}>
          <Text strong>{record.entity_type}</Text>
          <Text type="secondary">ID: {record.entity_id}</Text>
        </Space>
      ),
    },
    {
      title: 'Field',
      dataIndex: 'field_name',
      key: 'field_name',
      render: (text) => <Tag color="geekblue">{text}</Tag>,
    },
    {
      title: 'Missing Language',
      dataIndex: 'language',
      key: 'language',
      render: (lang) => {
        const langInfo = LANGUAGES[lang] || { name: lang, flag: '🌐', color: 'default' };
        return (
          <Space>
            <span>{langInfo.flag}</span>
            <Tag color={langInfo.color}>{langInfo.name}</Tag>
          </Space>
        );
      },
    },
    {
      title: 'Priority',
      dataIndex: 'priority',
      key: 'priority',
      render: (priority) => (
        <Tag color={priority === 'high' ? 'red' : 'orange'}>
          {priority.toUpperCase()}
        </Tag>
      ),
    },
    {
      title: 'Actions',
      key: 'actions',
      render: (_, record) => (
        <Button
          type="primary"
          size="small"
          icon={<PlusOutlined />}
          onClick={() => handleCreateFromMissing(record)}
        >
          Add Translation
        </Button>
      ),
    },
  ];

  // Handler functions
  const handleEdit = (record) => {
    setSelectedTranslation(record);
    form.setFieldsValue(record);
    setIsEditModalVisible(true);
  };

  const handleCreateFromMissing = (record) => {
    form.setFieldsValue({
      entity_type: record.entity_type,
      entity_id: record.entity_id,
      field_name: record.field_name,
      language: record.language,
      is_active: true
    });
    setIsCreateModalVisible(true);
  };

  const handleCreateSubmit = (values) => {
    createTranslationMutation.mutate(values);
  };

  const handleEditSubmit = (values) => {
    updateTranslationMutation.mutate({
      id: selectedTranslation.id,
      data: values
    });
  };

  const handleSyncSubmit = (values) => {
    syncTranslationsMutation.mutate({
      category: values.category,
      data: {}
    });
  };

  const handleExport = async (format = 'json') => {
    try {
      const blob = await adminService.exportTranslations({
        format,
        category: categoryFilter || undefined,
        language: languageFilter || undefined
      });
      
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `translations.${format}`;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
      
      message.success('Export completed successfully');
    } catch (error) {
      message.error('Failed to export translations');
    }
  };

  const handleTableChange = (pag) => {
    setPagination({
      current: pag.current,
      pageSize: pag.pageSize,
    });
  };

  // Render completion statistics
  const renderCompletionStats = () => {
    if (completionLoading || !completionData || !completionData.data) return null;

    const { overall_stats, completion_stats } = completionData.data;

    return (
      <div>
        <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
          <Col span={6}>
            <Card>
              <Statistic
                title="Overall Completion"
                value={overall_stats.overall_completion_percentage}
                precision={1}
                suffix="%"
                prefix={<PercentageOutlined />}
              />
              <Progress 
                percent={overall_stats.overall_completion_percentage} 
                size="small" 
                status={overall_stats.overall_completion_percentage > 80 ? 'success' : 'active'}
              />
            </Card>
          </Col>
          <Col span={6}>
            <Card>
              <Statistic
                title="English"
                value={overall_stats.language_breakdown.en.percentage}
                precision={1}
                suffix="%"
                valueStyle={{ color: '#1890ff' }}
              />
              <Text type="secondary">{overall_stats.language_breakdown.en.translated} / {overall_stats.language_breakdown.en.total}</Text>
            </Card>
          </Col>
          <Col span={6}>
            <Card>
              <Statistic
                title="Uzbek"
                value={overall_stats.language_breakdown.uz.percentage}
                precision={1}
                suffix="%"
                valueStyle={{ color: '#52c41a' }}
              />
              <Text type="secondary">{overall_stats.language_breakdown.uz.translated} / {overall_stats.language_breakdown.uz.total}</Text>
            </Card>
          </Col>
          <Col span={6}>
            <Card>
              <Statistic
                title="Russian"
                value={overall_stats.language_breakdown.ru.percentage}
                precision={1}
                suffix="%"
                valueStyle={{ color: '#f5222d' }}
              />
              <Text type="secondary">{overall_stats.language_breakdown.ru.translated} / {overall_stats.language_breakdown.ru.total}</Text>
            </Card>
          </Col>
        </Row>

        {completion_stats && completion_stats.length > 0 && (
          <Card title="Completion by Entity Type" style={{ marginBottom: 24 }}>
            <Row gutter={[16, 16]}>
              {completion_stats.map((stat) => (
                <Col span={8} key={stat.entity_type}>
                  <Card size="small">
                    <Space direction="vertical" size={0} style={{ width: '100%' }}>
                      <Text strong>{stat.entity_type}</Text>
                      <Progress 
                        percent={stat.completion_percentage} 
                        size="small"
                        format={() => `${stat.completion_percentage}%`}
                      />
                      <Text type="secondary">
                        {stat.total_actual_translations} / {stat.total_possible_translations} translations
                      </Text>
                    </Space>
                  </Card>
                </Col>
              ))}
            </Row>
          </Card>
        )}
      </div>
    );
  };

  return (
    <div>
      <div style={{ marginBottom: 24 }}>
        <Title level={2}>
          <TranslationOutlined /> Translation Management
        </Title>
        <Paragraph>
          Manage multilingual content for all translatable entities in the system.
          Track completion progress and maintain translations across English, Uzbek, and Russian.
        </Paragraph>
      </div>

      <Tabs activeKey={activeTab} onChange={setActiveTab} style={{ marginBottom: 24 }}>
        <TabPane tab={<span><TranslationOutlined />Translations</span>} key="translations">
          <Card>
            {/* Filters and Actions */}
            <div style={{ marginBottom: 16 }}>
              <Space wrap>
                <Input
                  placeholder="Search translations..."
                  prefix={<SearchOutlined />}
                  value={searchText}
                  onChange={(e) => setSearchText(e.target.value)}
                  style={{ width: 250 }}
                  allowClear
                />
                
                <Select
                  placeholder="Category"
                  value={categoryFilter}
                  onChange={setCategoryFilter}
                  style={{ width: 150 }}
                  allowClear
                >
                  {CATEGORIES.map(category => (
                    <Option key={category} value={category}>
                      {category}
                    </Option>
                  ))}
                </Select>

                <Select
                  placeholder="Language"
                  value={languageFilter}
                  onChange={setLanguageFilter}
                  style={{ width: 120 }}
                  allowClear
                >
                  {Object.entries(LANGUAGES).map(([code, info]) => (
                    <Option key={code} value={code}>
                      {info.flag} {info.name}
                    </Option>
                  ))}
                </Select>
              </Space>

              <div style={{ float: 'right' }}>
                <Space>
                  <Button
                    type="primary"
                    icon={<PlusOutlined />}
                    onClick={() => setIsCreateModalVisible(true)}
                  >
                    Add Translation
                  </Button>
                  
                  <Dropdown
                    menu={{
                      items: [
                        {
                          key: 'export-json',
                          icon: <DownloadOutlined />,
                          label: 'Export JSON',
                          onClick: () => handleExport('json')
                        },
                        {
                          key: 'export-csv',
                          icon: <DownloadOutlined />,
                          label: 'Export CSV',
                          onClick: () => handleExport('csv')
                        },
                        {
                          key: 'import',
                          icon: <UploadOutlined />,
                          label: 'Import',
                          onClick: () => setIsImportModalVisible(true)
                        },
                        {
                          key: 'sync',
                          icon: <SyncOutlined />,
                          label: 'Sync Entities',
                          onClick: () => setIsSyncModalVisible(true)
                        }
                      ]
                    }}
                  >
                    <Button icon={<MoreOutlined />} />
                  </Dropdown>
                </Space>
              </div>
              <div style={{ clear: 'both' }} />
            </div>

            <Table
              columns={translationColumns}
              dataSource={translationsData?.data?.translations || []}
              loading={translationsLoading}
              rowKey="id"
              pagination={{
                current: pagination.current,
                pageSize: pagination.pageSize,
                total: translationsData?.meta?.total || 0,
                showSizeChanger: true,
                showQuickJumper: true,
                showTotal: (total, range) => `${range[0]}-${range[1]} of ${total} translations`,
              }}
              onChange={handleTableChange}
              scroll={{ x: 1200 }}
            />
          </Card>
        </TabPane>

        <TabPane tab={<span><PercentageOutlined />Completion</span>} key="completion">
          {renderCompletionStats()}
        </TabPane>

        <TabPane tab={<span><ExclamationCircleOutlined />Missing</span>} key="missing">
          <Card>
            <div style={{ marginBottom: 16 }}>
              {missingData?.data?.summary && (
                <Alert
                  message={`${missingData.data.summary.total_missing} missing translations found`}
                  description={`${missingData.data.summary.high_priority} high priority, ${missingData.data.summary.medium_priority} medium priority`}
                  type="warning"
                  showIcon
                  style={{ marginBottom: 16 }}
                />
              )}
            </div>

            <Table
              columns={missingColumns}
              dataSource={missingData?.data?.missing_translations || []}
              loading={missingLoading}
              rowKey={(record) => `${record.entity_type}-${record.entity_id}-${record.field_name}-${record.language}`}
              pagination={{
                total: missingData?.meta?.total || 0,
                showSizeChanger: true,
                showTotal: (total, range) => `${range[0]}-${range[1]} of ${total} missing`,
              }}
            />
          </Card>
        </TabPane>
      </Tabs>

      {/* Create Translation Modal */}
      <Modal
        title="Create Translation"
        open={isCreateModalVisible}
        onCancel={() => {
          setIsCreateModalVisible(false);
          form.resetFields();
        }}
        footer={null}
        width={600}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={handleCreateSubmit}
        >
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                label="Category"
                name="category"
                rules={[{ required: true, message: 'Category is required' }]}
              >
                <Select placeholder="Select category">
                  {CATEGORIES.map(category => (
                    <Option key={category} value={category}>
                      {category}
                    </Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                label="Language"
                name="language"
                rules={[{ required: true, message: 'Language is required' }]}
              >
                <Select placeholder="Select language">
                  {Object.entries(LANGUAGES).map(([code, info]) => (
                    <Option key={code} value={code}>
                      {info.flag} {info.name}
                    </Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
          </Row>

          <Form.Item
            label="Key"
            name="key"
            rules={[{ required: true, message: 'Key is required' }]}
          >
            <Input placeholder="e.g., telegram.welcome_message" />
          </Form.Item>

          <Form.Item
            label="Value"
            name="value"
            rules={[{ required: true, message: 'Value is required' }]}
          >
            <TextArea
              rows={4}
              placeholder="Enter translation value..."
              showCount
              maxLength={5000}
            />
          </Form.Item>

          <Form.Item
            label="Active"
            name="is_active"
            valuePropName="checked"
            initialValue={true}
          >
            <Switch />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => {
                setIsCreateModalVisible(false);
                form.resetFields();
              }}>
                Cancel
              </Button>
              <Button 
                type="primary" 
                htmlType="submit"
                loading={createTranslationMutation.isLoading}
              >
                Create Translation
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* Edit Translation Modal */}
      <Modal
        title="Edit Translation"
        open={isEditModalVisible}
        onCancel={() => {
          setIsEditModalVisible(false);
          form.resetFields();
          setSelectedTranslation(null);
        }}
        footer={null}
        width={600}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={handleEditSubmit}
        >
          <Alert
            message={`Editing: ${selectedTranslation?.key} (${LANGUAGES[selectedTranslation?.language]?.name})`}
            type="info"
            style={{ marginBottom: 16 }}
          />

          <Form.Item
            label="Value"
            name="value"
            rules={[{ required: true, message: 'Value is required' }]}
          >
            <TextArea
              rows={6}
              placeholder="Enter translation value..."
              showCount
              maxLength={5000}
            />
          </Form.Item>

          <Form.Item
            label="Active"
            name="is_active"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => {
                setIsEditModalVisible(false);
                form.resetFields();
                setSelectedTranslation(null);
              }}>
                Cancel
              </Button>
              <Button 
                type="primary" 
                htmlType="submit"
                loading={updateTranslationMutation.isLoading}
              >
                Update Translation
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* Sync Modal */}
      <Modal
        title="Sync Entity Translations"
        open={isSyncModalVisible}
        onCancel={() => {
          setIsSyncModalVisible(false);
          syncForm.resetFields();
        }}
        footer={null}
      >
        <Form
          form={syncForm}
          layout="vertical"
          onFinish={handleSyncSubmit}
        >
          <Alert
            message="Sync will create baseline translations for categories that don't have them yet."
            type="info"
            style={{ marginBottom: 16 }}
          />

          <Form.Item
            label="Category"
            name="category"
            rules={[{ required: true, message: 'Category is required' }]}
          >
            <Select placeholder="Select category to sync">
              {CATEGORIES.map(category => (
                <Option key={category} value={category}>
                  {category}
                </Option>
              ))}
            </Select>
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => {
                setIsSyncModalVisible(false);
                syncForm.resetFields();
              }}>
                Cancel
              </Button>
              <Button 
                type="primary" 
                htmlType="submit"
                loading={syncTranslationsMutation.isLoading}
              >
                Sync Translations
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* Import Modal */}
      <Modal
        title="Import Translations"
        open={isImportModalVisible}
        onCancel={() => {
          setIsImportModalVisible(false);
          importForm.resetFields();
        }}
        footer={null}
        width={600}
      >
        <Alert
          message="Import translations from JSON data"
          description="Upload a JSON file or paste JSON data containing translation records."
          type="info"
          style={{ marginBottom: 16 }}
        />

        <Form
          form={importForm}
          layout="vertical"
          onFinish={(values) => {
            try {
              const translations = JSON.parse(values.translations_json);
              importTranslationsMutation.mutate({
                translations,
                update_existing: values.update_existing
              });
            } catch (error) {
              message.error('Invalid JSON format');
            }
          }}
        >
          <Form.Item
            label="JSON Data"
            name="translations_json"
            rules={[{ required: true, message: 'JSON data is required' }]}
          >
            <TextArea
              rows={10}
              placeholder='Paste JSON data here, e.g., [{"key": "telegram.welcome", "value": "Welcome!", "language": "en", "category": "telegram"}]'
            />
          </Form.Item>

          <Form.Item
            label="Update Existing"
            name="update_existing"
            valuePropName="checked"
            initialValue={false}
          >
            <Switch />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => {
                setIsImportModalVisible(false);
                importForm.resetFields();
              }}>
                Cancel
              </Button>
              <Button 
                type="primary" 
                htmlType="submit"
                loading={importTranslationsMutation.isLoading}
              >
                Import Translations
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default Translations;