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
import adminService from '../services/adminService';
import useResponsive from '../hooks/useResponsive';

const { Option } = Select;

const Users = () => {
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
        message.success('User status updated successfully');
        queryClient.invalidateQueries('users');
      },
      onError: (error) => {
        message.error('Failed to update user status');
      }
    }
  );

  const columns = [
    {
      title: 'User',
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
                title="Verified"
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
      title: 'Contact',
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
      title: 'Role',
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
      title: 'Status',
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
              {status.toUpperCase()}
            </Tag>
            {record.is_bot_active && record.telegram_id && (
              <Tag color="processing" size="small">
                Bot
              </Tag>
            )}
          </Space>
        );
      }
    },
    {
      title: 'Activity',
      key: 'activity',
      width: responsive.isMobileDevice ? 120 : 150,
      render: (text, record) => (
        <Space direction="vertical" size={2}>
          <div style={{ fontSize: '11px' }}>
            Created: {new Date(record.created_at).toLocaleDateString()}
          </div>
          <div style={{ fontSize: '11px' }}>
            Login: {record.last_login ? new Date(record.last_login).toLocaleDateString() : 'Never'}
          </div>
          {record.last_bot_interaction && (
            <div style={{ fontSize: '11px', color: '#1890ff' }}>
              Bot: {new Date(record.last_bot_interaction).toLocaleDateString()}
            </div>
          )}
        </Space>
      )
    },
    {
      title: 'Actions',
      key: 'actions',
      width: 60,
      fixed: 'right',
      render: (_, record) => (
        <Dropdown
          menu={{
            items: [
              {
                key: 'view',
                label: 'View Details',
                onClick: () => handleViewUser(record)
              },
              {
                key: 'activate',
                label: 'Activate',
                disabled: record.status === 'active',
                onClick: () => handleStatusChange(record.id, 'active')
              },
              {
                key: 'suspend',
                label: 'Suspend',
                disabled: record.status === 'suspended',
                onClick: () => handleStatusChange(record.id, 'suspended')
              },
              {
                key: 'ban',
                label: 'Ban',
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
      title: `Change user status to ${status}?`,
      content: 'Are you sure you want to change this user\'s status?',
      onOk: () => {
        updateUserMutation.mutate({
          userId,
          status,
          reason: `Status changed to ${status} by admin`
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
                placeholder="Search users..."
                allowClear
                onSearch={handleSearch}
                style={{ 
                  minWidth: '240px',
                  maxWidth: responsive.isMobileDevice ? '100%' : '300px',
                  minHeight: responsive.isTouchDevice ? '40px' : '32px'
                }}
              />
              <Select
                placeholder="Filter by status"
                allowClear
                onChange={handleStatusFilter}
                style={{ 
                  width: '150px',
                  minHeight: responsive.isTouchDevice ? '40px' : '32px'
                }}
              >
                <Option value="active">Active</Option>
                <Option value="inactive">Inactive</Option>
                <Option value="suspended">Suspended</Option>
                <Option value="banned">Banned</Option>
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
                Export
              </Button>
              <Button 
                type="primary" 
                icon={<PlusOutlined />}
                style={{ 
                  minHeight: responsive.isTouchDevice ? '40px' : '32px'
                }}
              >
                Add User
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
                ? `${total} total`
                : `${range[0]}-${range[1]} of ${total} users`,
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
            <span>User Details</span>
            {selectedUser?.telegram_id && (
              <Tag color="blue" icon={<MessageOutlined />} size="small">
                Telegram
              </Tag>
            )}
            {selectedUser?.is_verified && (
              <Tag color="success" icon={<CheckCircleOutlined />} size="small">
                Verified
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
              title="Basic Information" 
              size="small"
              style={{ marginBottom: 12 }}
            >
              <Row gutter={[16, 12]}>
                <Col xs={24} sm={12}>
                  <div style={{ marginBottom: 8 }}>
                    <strong>Name:</strong> {selectedUser.first_name} {selectedUser.last_name}
                  </div>
                  <div style={{ marginBottom: 8 }}>
                    <strong>Email:</strong> 
                    <div style={{ 
                      wordBreak: 'break-word', 
                      fontSize: '13px',
                      marginTop: '2px'
                    }}>
                      {selectedUser.email}
                    </div>
                  </div>
                  <div style={{ marginBottom: 8 }}>
                    <strong>Phone:</strong> {selectedUser.phone || 'N/A'}
                  </div>
                </Col>
                <Col xs={24} sm={12}>
                  <div style={{ marginBottom: 8 }}>
                    <strong>Role:</strong> 
                    <Tag color={selectedUser.role === 'admin' ? 'red' : 'blue'} style={{ marginLeft: 8 }}>
                      {selectedUser.role.toUpperCase()}
                    </Tag>
                  </div>
                  <div style={{ marginBottom: 8 }}>
                    <strong>Status:</strong> 
                    <Tag color="green" style={{ marginLeft: 8 }}>
                      {selectedUser.status.toUpperCase()}
                    </Tag>
                  </div>
                  <div style={{ marginBottom: 8 }}>
                    <strong>Registration Source:</strong> 
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
                    <strong>Language:</strong> {selectedUser.preferred_language || 'N/A'}
                  </div>
                </Col>
              </Row>
            </Card>

            {/* Telegram Info */}
            {selectedUser.telegram_id && (
              <Card 
                title="Telegram Information" 
                size="small"
                style={{ marginBottom: 12 }}
              >
                <Row gutter={[16, 12]}>
                  <Col xs={24} sm={12}>
                    <div style={{ marginBottom: 8 }}>
                      <strong>Telegram ID:</strong> {selectedUser.telegram_id}
                    </div>
                    <div style={{ marginBottom: 8 }}>
                      <strong>Username:</strong> {selectedUser.telegram_username ? `@${selectedUser.telegram_username}` : 'N/A'}
                    </div>
                  </Col>
                  <Col xs={24} sm={12}>
                    <div style={{ marginBottom: 8 }}>
                      <strong>Bot Active:</strong> 
                      <Tag color={selectedUser.is_bot_active ? 'processing' : 'default'} style={{ marginLeft: 8 }}>
                        {selectedUser.is_bot_active ? 'Active' : 'Inactive'}
                      </Tag>
                    </div>
                    <div style={{ marginBottom: 8 }}>
                      <strong>Last Bot Interaction:</strong> 
                      <div style={{ fontSize: '12px', marginTop: '2px' }}>
                        {selectedUser.last_bot_interaction ? new Date(selectedUser.last_bot_interaction).toLocaleString() : 'Never'}
                      </div>
                    </div>
                  </Col>
                </Row>
              </Card>
            )}

            {/* Activity Info */}
            <Card title="Activity Information" size="small">
              <Row gutter={[16, 12]}>
                <Col xs={24} sm={12}>
                  <div style={{ marginBottom: 8 }}>
                    <strong>Created:</strong> 
                    <div style={{ fontSize: '12px', marginTop: '2px' }}>
                      {new Date(selectedUser.created_at).toLocaleString()}
                    </div>
                  </div>
                  <div style={{ marginBottom: 8 }}>
                    <strong>Updated:</strong> 
                    <div style={{ fontSize: '12px', marginTop: '2px' }}>
                      {selectedUser.updated_at ? new Date(selectedUser.updated_at).toLocaleString() : 'N/A'}
                    </div>
                  </div>
                </Col>
                <Col xs={24} sm={12}>
                  <div style={{ marginBottom: 8 }}>
                    <strong>Last Login:</strong> 
                    <div style={{ fontSize: '12px', marginTop: '2px' }}>
                      {selectedUser.last_login ? new Date(selectedUser.last_login).toLocaleString() : 'Never'}
                    </div>
                  </div>
                  <div style={{ marginBottom: 8 }}>
                    <strong>Email Verified:</strong> {selectedUser.email_verified ? 'Yes' : 'No'}
                  </div>
                  <div style={{ marginBottom: 8 }}>
                    <strong>Phone Verified:</strong> {selectedUser.phone_verified ? 'Yes' : 'No'}
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