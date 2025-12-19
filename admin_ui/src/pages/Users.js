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
  message,
  Row,
  Col
} from 'antd';
import {
  SearchOutlined,
  UserOutlined,
  MoreOutlined,
  PlusOutlined,
  ExportOutlined,
  GlobalOutlined,
  MessageOutlined,
  CheckCircleOutlined
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient } from 'react-query';
import { useTranslation } from 'react-i18next';
import adminService from '../services/adminService';
import useResponsive from '../hooks/useResponsive';

const { Option } = Select;

const Users = () => {
  const { t } = useTranslation();
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [selectedUser, setSelectedUser] = useState(null);
  const [isModalVisible, setIsModalVisible] = useState(false);
  const [pagination, setPagination] = useState({ page: 1, per_page: 20 });
  const responsive = useResponsive();

  const queryClient = useQueryClient();

  // Fetch users
  const { data, isLoading } = useQuery(
    ['users', pagination, searchText, statusFilter],
    () => adminService.getUsers({
      page: pagination.page,
      per_page: pagination.per_page,
      search: searchText,
      status: statusFilter
    }),
    {
      keepPreviousData: true
    }
  );

  // Update user status mutation
  const updateUserMutation = useMutation(
    ({ userId, status, reason }) => adminService.updateUserStatus(userId, status, reason),
    {
      onSuccess: () => {
        message.success(t('ui.users.status_updated_success'));
        queryClient.invalidateQueries('users');
      },
      onError: (error) => {
        message.error(t('ui.users.status_update_failed'));
      }
    }
  );

  const columns = [
    {
      title: t('ui.users.user'),
      dataIndex: 'first_name',
      key: 'user',
      width: responsive.isMobileDevice ? 200 : 300,
      render: (text, record) => (
        <Space direction="vertical" size={4}>
          <div style={{
            display: 'flex',
            alignItems: 'center',
            flexWrap: 'wrap',
            gap: '4px'
          }}>
            <UserOutlined style={{ color: '#1890ff' }} />
            <span style={{
              fontWeight: 600,
              fontSize: responsive.getFontSize('14px', '14px', '14px')
            }}>
              {`${record.first_name} ${record.last_name}`}
            </span>
            {record.telegram_id && (
              <Tag
                color="blue"
                icon={<MessageOutlined />}
                size="small"
              >
                {responsive.isMobileDevice ? 'TG' : 'Telegram'}
              </Tag>
            )}
            {record.is_verified && (
              <CheckCircleOutlined
                style={{ color: '#52c41a' }}
                title={t('ui.users.verified')}
              />
            )}
          </div>
          <div style={{
            fontSize: '12px',
            color: '#666',
            wordBreak: 'break-word'
          }}>
            {record.email}
          </div>
          {record.telegram_id && (
            <div style={{
              fontSize: '11px',
              color: '#1890ff'
            }}>
              TG: {record.telegram_id}
              {record.telegram_username && ` (@${record.telegram_username})`}
            </div>
          )}
        </Space>
      )
    },
    {
      title: t('ui.users.contact'),
      key: 'contact',
      width: responsive.isMobileDevice ? 150 : 200,
      render: (text, record) => (
        <Space direction="vertical" size={4}>
          {record.phone && (
            <div style={{ fontSize: '14px' }}>{record.phone}</div>
          )}
          <div>
            <Tag
              color={record.registration_source === 'telegram' ? 'blue' : 'green'}
              size="small"
              icon={record.registration_source === 'telegram' ? <MessageOutlined /> : <GlobalOutlined />}
            >
              {record.registration_source === 'telegram' ? 'Telegram' : 'Web'}
            </Tag>
          </div>
        </Space>
      )
    },
    {
      title: t('ui.users.role'),
      dataIndex: 'role',
      key: 'role',
      width: 80,
      render: (role) => (
        <Tag color={role === 'admin' ? 'red' : 'blue'} size="small">
          {role.toUpperCase()}
        </Tag>
      )
    },
    {
      title: t('ui.users.status'),
      dataIndex: 'status',
      key: 'status',
      width: responsive.isMobileDevice ? 100 : 120,
      render: (status, record) => {
        const colors = {
          active: 'green',
          inactive: 'red',
          suspended: 'orange',
          banned: 'red'
        };
        return (
          <Space direction="vertical" size={2}>
            <Tag color={colors[status]} size="small">
              {t(`ui.users.status_${status}`)}
            </Tag>
            {record.is_bot_active && record.telegram_id && (
              <Tag color="processing" size="small">
                {t('ui.users.bot')}
              </Tag>
            )}
          </Space>
        );
      }
    },
    {
      title: t('ui.users.activity'),
      key: 'activity',
      width: responsive.isMobileDevice ? 120 : 150,
      render: (text, record) => (
        <Space direction="vertical" size={2}>
          <div style={{ fontSize: '11px' }}>
            {t('ui.users.created')}: {new Date(record.created_at).toLocaleDateString()}
          </div>
          <div style={{ fontSize: '11px' }}>
            {t('ui.users.login')}: {record.last_login ? new Date(record.last_login).toLocaleDateString() : t('ui.users.never')}
          </div>
          {record.last_bot_interaction && (
            <div style={{ fontSize: '11px', color: '#1890ff' }}>
              {t('ui.users.bot')}: {new Date(record.last_bot_interaction).toLocaleDateString()}
            </div>
          )}
        </Space>
      )
    },
    {
      title: t('ui.users.actions'),
      key: 'actions',
      width: 60,
      fixed: 'right',
      render: (_, record) => (
        <Dropdown
          menu={{
            items: [
              {
                key: 'view',
                label: t('ui.users.view_details'),
                onClick: () => handleViewUser(record)
              },
              {
                key: 'activate',
                label: t('ui.users.activate'),
                disabled: record.status === 'active',
                onClick: () => handleStatusChange(record.id, 'active')
              },
              {
                key: 'suspend',
                label: t('ui.users.suspend'),
                disabled: record.status === 'suspended',
                onClick: () => handleStatusChange(record.id, 'suspended')
              },
              {
                key: 'ban',
                label: t('ui.users.ban'),
                danger: true,
                disabled: record.status === 'banned',
                onClick: () => handleStatusChange(record.id, 'banned')
              }
            ]
          }}
          trigger={['click']}
        >
          <Button
            type="text"
            icon={<MoreOutlined />}
            size={responsive.isMobileDevice ? 'large' : 'middle'}
          />
        </Dropdown>
      )
    }
  ];

  const handleViewUser = (user) => {
    setSelectedUser(user);
    setIsModalVisible(true);
  };

  const handleStatusChange = (userId, status) => {
    Modal.confirm({
      title: `${t('ui.users.change_status_title')} ${t(`ui.users.status_${status}`).toLowerCase()}?`,
      content: t('ui.users.change_status_confirm'),
      onOk: () => {
        updateUserMutation.mutate({
          userId,
          status,
          reason: `${t('ui.users.status_changed_by_admin')}`
        });
      }
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

  const handleStatusFilter = (value) => {
    setStatusFilter(value);
    setPagination({ ...pagination, page: 1 });
  };

  return (
    <div>
      <Card>
        {/* Header - Universal Responsive Layout */}
        <Row 
          gutter={[16, 16]} 
          align="middle"
          justify="space-between"
          style={{ marginBottom: 20 }}
        >
          {/* Search and Filters */}
          <Col xs={24} sm={24} md={14} lg={14} xl={16}>
            <Space 
              wrap
              size="middle" 
              style={{ width: '100%' }}
            >
              <Input.Search
                placeholder={t('ui.users.search_placeholder')}
                allowClear
                onSearch={handleSearch}
                style={{
                  minWidth: '240px',
                  maxWidth: responsive.isMobileDevice ? '100%' : '300px',
                  minHeight: responsive.isTouchDevice ? '40px' : '32px'
                }}
              />
              <Select
                placeholder={t('ui.users.filter_by_status')}
                allowClear
                onChange={handleStatusFilter}
                style={{
                  width: '150px',
                  minHeight: responsive.isTouchDevice ? '40px' : '32px'
                }}
              >
                <Option value="active">{t('ui.users.status_active')}</Option>
                <Option value="inactive">{t('ui.users.status_inactive')}</Option>
                <Option value="suspended">{t('ui.users.status_suspended')}</Option>
                <Option value="banned">{t('ui.users.status_banned')}</Option>
              </Select>
            </Space>
          </Col>

          {/* Action Buttons */}
          <Col xs={24} sm={24} md={10} lg={10} xl={8}>
            <Space 
              wrap
              size="middle" 
              style={{ 
                width: '100%',
                justifyContent: responsive.isMobileDevice ? 'center' : 'flex-end'
              }}
            >
              <Button
                icon={<ExportOutlined />}
                style={{
                  minHeight: responsive.isTouchDevice ? '40px' : '32px'
                }}
              >
                {t('ui.users.export')}
              </Button>
              <Button
                type="primary"
                icon={<PlusOutlined />}
                style={{
                  minHeight: responsive.isTouchDevice ? '40px' : '32px'
                }}
              >
                {t('ui.users.add_user')}
              </Button>
            </Space>
          </Col>
        </Row>

        {/* Table */}
        <Table
          columns={columns}
          dataSource={data?.data?.items || []}
          loading={isLoading}
          rowKey="id"
          pagination={{
            current: pagination.page,
            pageSize: pagination.per_page,
            total: data?.meta?.total || 0,
            showSizeChanger: !responsive.isMobileDevice,
            showQuickJumper: !responsive.isMobileDevice,
            showTotal: (total, range) =>
              responsive.isMobileDevice
                ? `${total} ${t('ui.users.total')}`
                : `${range[0]}-${range[1]} of ${total} ${t('ui.users.pagination_text')}`,
            size: responsive.isMobileDevice ? 'small' : 'default'
          }}
          onChange={handleTableChange}
          className="admin-table"
          scroll={{ 
            x: responsive.isMobileDevice ? 800 : 'auto',
            y: responsive.isMobileDevice ? 400 : undefined
          }}
          size={responsive.isMobileDevice ? 'small' : 'middle'}
        />
      </Card>

      {/* User Details Modal - Responsive */}
      <Modal
        title={
          <Space wrap size="small">
            <UserOutlined />
            <span>{t('ui.users.user_details')}</span>
            {selectedUser?.telegram_id && (
              <Tag color="blue" icon={<MessageOutlined />} size="small">
                Telegram
              </Tag>
            )}
            {selectedUser?.is_verified && (
              <Tag color="success" icon={<CheckCircleOutlined />} size="small">
                {t('ui.users.verified')}
              </Tag>
            )}
          </Space>
        }
        open={isModalVisible}
        onCancel={() => setIsModalVisible(false)}
        footer={null}
        width={responsive.isMobileDevice ? '95%' : 700}
        style={{
          maxWidth: responsive.isMobileDevice ? 'none' : 700,
          top: responsive.isMobileDevice ? 20 : 100
        }}
      >
        {selectedUser && (
          <div>
            {/* Basic Info */}
            <Card
              title={t('ui.users.basic_information')}
              size="small"
              style={{ marginBottom: 12 }}
            >
              <Row gutter={[16, 12]}>
                <Col xs={24} sm={12}>
                  <div style={{ marginBottom: 8 }}>
                    <strong>{t('ui.users.name')}:</strong> {selectedUser.first_name} {selectedUser.last_name}
                  </div>
                  <div style={{ marginBottom: 8 }}>
                    <strong>{t('ui.users.email')}:</strong>
                    <div style={{
                      wordBreak: 'break-word',
                      fontSize: '13px',
                      marginTop: '2px'
                    }}>
                      {selectedUser.email}
                    </div>
                  </div>
                  <div style={{ marginBottom: 8 }}>
                    <strong>{t('ui.users.phone')}:</strong> {selectedUser.phone || t('ui.users.na')}
                  </div>
                </Col>
                <Col xs={24} sm={12}>
                  <div style={{ marginBottom: 8 }}>
                    <strong>{t('ui.users.role')}:</strong>
                    <Tag color={selectedUser.role === 'admin' ? 'red' : 'blue'} style={{ marginLeft: 8 }}>
                      {selectedUser.role.toUpperCase()}
                    </Tag>
                  </div>
                  <div style={{ marginBottom: 8 }}>
                    <strong>{t('ui.users.status')}:</strong>
                    <Tag color="green" style={{ marginLeft: 8 }}>
                      {t(`ui.users.status_${selectedUser.status}`)}
                    </Tag>
                  </div>
                  <div style={{ marginBottom: 8 }}>
                    <strong>{t('ui.users.registration_source')}:</strong>
                    <Tag
                      color={selectedUser.registration_source === 'telegram' ? 'blue' : 'green'}
                      style={{ marginLeft: 8 }}
                      icon={selectedUser.registration_source === 'telegram' ? <MessageOutlined /> : <GlobalOutlined />}
                      size="small"
                    >
                      {selectedUser.registration_source === 'telegram' ? 'Telegram' : 'Web'}
                    </Tag>
                  </div>
                  <div style={{ marginBottom: 8 }}>
                    <strong>{t('ui.users.language')}:</strong> {selectedUser.preferred_language || t('ui.users.na')}
                  </div>
                </Col>
              </Row>
            </Card>

            {/* Telegram Info */}
            {selectedUser.telegram_id && (
              <Card
                title={t('ui.users.telegram_information')}
                size="small"
                style={{ marginBottom: 12 }}
              >
                <Row gutter={[16, 12]}>
                  <Col xs={24} sm={12}>
                    <div style={{ marginBottom: 8 }}>
                      <strong>{t('ui.users.telegram_id')}:</strong> {selectedUser.telegram_id}
                    </div>
                    <div style={{ marginBottom: 8 }}>
                      <strong>{t('ui.users.username')}:</strong> {selectedUser.telegram_username ? `@${selectedUser.telegram_username}` : t('ui.users.na')}
                    </div>
                  </Col>
                  <Col xs={24} sm={12}>
                    <div style={{ marginBottom: 8 }}>
                      <strong>{t('ui.users.bot_active')}:</strong>
                      <Tag color={selectedUser.is_bot_active ? 'processing' : 'default'} style={{ marginLeft: 8 }}>
                        {selectedUser.is_bot_active ? t('ui.users.status_active') : t('ui.users.status_inactive')}
                      </Tag>
                    </div>
                    <div style={{ marginBottom: 8 }}>
                      <strong>{t('ui.users.last_bot_interaction')}:</strong>
                      <div style={{ fontSize: '12px', marginTop: '2px' }}>
                        {selectedUser.last_bot_interaction ? new Date(selectedUser.last_bot_interaction).toLocaleString() : t('ui.users.never')}
                      </div>
                    </div>
                  </Col>
                </Row>
              </Card>
            )}

            {/* Activity Info */}
            <Card title={t('ui.users.activity_information')} size="small">
              <Row gutter={[16, 12]}>
                <Col xs={24} sm={12}>
                  <div style={{ marginBottom: 8 }}>
                    <strong>{t('ui.users.created')}:</strong>
                    <div style={{ fontSize: '12px', marginTop: '2px' }}>
                      {new Date(selectedUser.created_at).toLocaleString()}
                    </div>
                  </div>
                  <div style={{ marginBottom: 8 }}>
                    <strong>{t('ui.users.updated')}:</strong>
                    <div style={{ fontSize: '12px', marginTop: '2px' }}>
                      {selectedUser.updated_at ? new Date(selectedUser.updated_at).toLocaleString() : t('ui.users.na')}
                    </div>
                  </div>
                </Col>
                <Col xs={24} sm={12}>
                  <div style={{ marginBottom: 8 }}>
                    <strong>{t('ui.users.last_login')}:</strong>
                    <div style={{ fontSize: '12px', marginTop: '2px' }}>
                      {selectedUser.last_login ? new Date(selectedUser.last_login).toLocaleString() : t('ui.users.never')}
                    </div>
                  </div>
                  <div style={{ marginBottom: 8 }}>
                    <strong>{t('ui.users.email_verified')}:</strong> {selectedUser.email_verified ? t('ui.users.yes') : t('ui.users.no')}
                  </div>
                  <div style={{ marginBottom: 8 }}>
                    <strong>{t('ui.users.phone_verified')}:</strong> {selectedUser.phone_verified ? t('ui.users.yes') : t('ui.users.no')}
                  </div>
                </Col>
              </Row>
            </Card>
          </div>
        )}
      </Modal>
    </div>
  );
};

export default Users;