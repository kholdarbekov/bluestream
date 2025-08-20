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
  message
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

const { Option } = Select;

const Users = () => {
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [selectedUser, setSelectedUser] = useState(null);
  const [isModalVisible, setIsModalVisible] = useState(false);
  const [pagination, setPagination] = useState({ page: 1, per_page: 20 });

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
      render: (text, record) => (
        <Space>
          <UserOutlined style={{ color: '#1890ff' }} />
          <div>
            <div>
              {`${record.first_name} ${record.last_name}`}
              {record.telegram_id && (
                <Tag
                  color="blue"
                  icon={<MessageOutlined />}
                  style={{ marginLeft: 8, fontSize: '11px' }}
                >
                  Telegram
                </Tag>
              )}
              {record.is_verified && (
                <CheckCircleOutlined 
                  style={{ color: '#52c41a', marginLeft: 4 }} 
                  title="Verified"
                />
              )}
            </div>
            <small style={{ color: '#666' }}>{record.email}</small>
            {record.telegram_id && (
              <div>
                <small style={{ color: '#1890ff' }}>
                  Telegram ID: {record.telegram_id}
                  {record.telegram_username && ` (@${record.telegram_username})`}
                </small>
              </div>
            )}
          </div>
        </Space>
      )
    },
    {
      title: 'Contact',
      key: 'contact',
      render: (text, record) => (
        <div>
          {record.phone && <div>{record.phone}</div>}
          <div style={{ fontSize: '12px', color: '#666' }}>
            Registered via: 
            <Tag 
              color={record.registration_source === 'telegram' ? 'blue' : 'green'}
              size="small"
              style={{ marginLeft: 4 }}
              icon={record.registration_source === 'telegram' ? <MessageOutlined /> : <GlobalOutlined />}
            >
              {record.registration_source === 'telegram' ? 'Telegram' : 'Web'}
            </Tag>
          </div>
        </div>
      )
    },
    {
      title: 'Role',
      dataIndex: 'role',
      key: 'role',
      render: (role) => (
        <Tag color={role === 'admin' ? 'red' : 'blue'}>
          {role.toUpperCase()}
        </Tag>
      )
    },
    {
      title: 'Status',
      dataIndex: 'status',
      key: 'status',
      render: (status, record) => {
        const colors = {
          active: 'green',
          inactive: 'red',
          suspended: 'orange',
          banned: 'red'
        };
        return (
          <div>
            <Tag color={colors[status]}>{status.toUpperCase()}</Tag>
            {record.is_bot_active && record.telegram_id && (
              <div>
                <Tag color="processing" size="small">
                  Bot Active
                </Tag>
              </div>
            )}
          </div>
        );
      }
    },
    {
      title: 'Activity',
      key: 'activity',
      render: (text, record) => (
        <div>
          <div style={{ fontSize: '12px' }}>
            Created: {new Date(record.created_at).toLocaleDateString()}
          </div>
          <div style={{ fontSize: '12px' }}>
            Last Login: {record.last_login ? new Date(record.last_login).toLocaleDateString() : 'Never'}
          </div>
          {record.last_bot_interaction && (
            <div style={{ fontSize: '12px', color: '#1890ff' }}>
              Bot Activity: {new Date(record.last_bot_interaction).toLocaleDateString()}
            </div>
          )}
        </div>
      )
    },
    {
      title: 'Actions',
      key: 'actions',
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
          <Button type="text" icon={<MoreOutlined />} />
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
        {/* Header */}
        <div className="table-actions">
          <Space>
            <Input.Search
              placeholder="Search users..."
              allowClear
              onSearch={handleSearch}
              style={{ width: 300 }}
            />
            <Select
              placeholder="Filter by status"
              allowClear
              onChange={handleStatusFilter}
              style={{ width: 150 }}
            >
              <Option value="active">Active</Option>
              <Option value="inactive">Inactive</Option>
              <Option value="suspended">Suspended</Option>
              <Option value="banned">Banned</Option>
            </Select>
          </Space>

          <Space>
            <Button icon={<ExportOutlined />}>
              Export
            </Button>
            <Button type="primary" icon={<PlusOutlined />}>
              Add User
            </Button>
          </Space>
        </div>

        {/* Table */}
        <Table
          columns={columns}
          dataSource={data?.users || []}
          loading={isLoading}
          rowKey="id"
          pagination={{
            current: pagination.page,
            pageSize: pagination.per_page,
            total: data?.pagination?.total || 0,
            showSizeChanger: true,
            showQuickJumper: true,
            showTotal: (total, range) =>
              `${range[0]}-${range[1]} of ${total} users`
          }}
          onChange={handleTableChange}
          className="admin-table"
        />
      </Card>

      {/* User Details Modal */}
      <Modal
        title={
          <Space>
            <UserOutlined />
            User Details
            {selectedUser?.telegram_id && (
              <Tag color="blue" icon={<MessageOutlined />}>
                Telegram User
              </Tag>
            )}
            {selectedUser?.is_verified && (
              <Tag color="success" icon={<CheckCircleOutlined />}>
                Verified
              </Tag>
            )}
          </Space>
        }
        open={isModalVisible}
        onCancel={() => setIsModalVisible(false)}
        footer={null}
        width={700}
      >
        {selectedUser && (
          <div>
            {/* Basic Info */}
            <Card title="Basic Information" style={{ marginBottom: 16 }}>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px' }}>
                <div>
                  <p><strong>Name:</strong> {selectedUser.first_name} {selectedUser.last_name}</p>
                  <p><strong>Full Name:</strong> {selectedUser.full_name || 'N/A'}</p>
                  <p><strong>Email:</strong> {selectedUser.email}</p>
                  <p><strong>Phone:</strong> {selectedUser.phone || 'N/A'}</p>
                </div>
                <div>
                  <p><strong>Role:</strong> 
                    <Tag color={selectedUser.role === 'admin' ? 'red' : 'blue'} style={{ marginLeft: 8 }}>
                      {selectedUser.role.toUpperCase()}
                    </Tag>
                  </p>
                  <p><strong>Status:</strong> 
                    <Tag color="green" style={{ marginLeft: 8 }}>
                      {selectedUser.status.toUpperCase()}
                    </Tag>
                  </p>
                  <p><strong>Registration Source:</strong> 
                    <Tag 
                      color={selectedUser.registration_source === 'telegram' ? 'blue' : 'green'}
                      style={{ marginLeft: 8 }}
                      icon={selectedUser.registration_source === 'telegram' ? <MessageOutlined /> : <GlobalOutlined />}
                    >
                      {selectedUser.registration_source === 'telegram' ? 'Telegram' : 'Web'}
                    </Tag>
                  </p>
                  <p><strong>Preferred Language:</strong> {selectedUser.preferred_language || 'N/A'}</p>
                </div>
              </div>
            </Card>

            {/* Telegram Info */}
            {selectedUser.telegram_id && (
              <Card title="Telegram Information" style={{ marginBottom: 16 }}>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px' }}>
                  <div>
                    <p><strong>Telegram ID:</strong> {selectedUser.telegram_id}</p>
                    <p><strong>Username:</strong> {selectedUser.telegram_username ? `@${selectedUser.telegram_username}` : 'N/A'}</p>
                    <p><strong>Telegram Name:</strong> {selectedUser.telegram_first_name || 'N/A'} {selectedUser.telegram_last_name || ''}</p>
                  </div>
                  <div>
                    <p><strong>Bot Active:</strong> 
                      <Tag color={selectedUser.is_bot_active ? 'processing' : 'default'} style={{ marginLeft: 8 }}>
                        {selectedUser.is_bot_active ? 'Active' : 'Inactive'}
                      </Tag>
                    </p>
                    <p><strong>Language Code:</strong> {selectedUser.telegram_language_code || 'N/A'}</p>
                    <p><strong>Last Bot Interaction:</strong> {selectedUser.last_bot_interaction ? new Date(selectedUser.last_bot_interaction).toLocaleString() : 'Never'}</p>
                  </div>
                </div>
              </Card>
            )}

            {/* Activity Info */}
            <Card title="Activity Information">
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px' }}>
                <div>
                  <p><strong>Created:</strong> {new Date(selectedUser.created_at).toLocaleString()}</p>
                  <p><strong>Updated:</strong> {selectedUser.updated_at ? new Date(selectedUser.updated_at).toLocaleString() : 'N/A'}</p>
                </div>
                <div>
                  <p><strong>Last Login:</strong> {selectedUser.last_login ? new Date(selectedUser.last_login).toLocaleString() : 'Never'}</p>
                  <p><strong>Email Verified:</strong> {selectedUser.email_verified ? 'Yes' : 'No'}</p>
                  <p><strong>Phone Verified:</strong> {selectedUser.phone_verified ? 'Yes' : 'No'}</p>
                </div>
              </div>
            </Card>
          </div>
        )}
      </Modal>
    </div>
  );
};

export default Users;