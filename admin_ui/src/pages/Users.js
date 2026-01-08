import React, { useState, useCallback } from 'react';
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
  Col,
  Checkbox,
  Divider
} from 'antd';
import {
  SearchOutlined,
  UserOutlined,
  MoreOutlined,
  PlusOutlined,
  ExportOutlined,
  GlobalOutlined,
  MessageOutlined,
  CheckCircleOutlined,
  EnvironmentOutlined,
  EditOutlined,
  DeleteOutlined,
  PhoneOutlined,
  MailOutlined
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient } from 'react-query';
import { useTranslation } from 'react-i18next';
import adminService from '../services/adminService';
import api from '../services/api';
import useResponsive from '../hooks/useResponsive';
import AddressMapPicker from '../components/AddressMapPicker';

const { Option } = Select;

const Users = () => {
  // Load users namespace for ui.users.* keys
  const { t } = useTranslation('users');
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [registrationMethodFilter, setRegistrationMethodFilter] = useState('');
  const [selectedUser, setSelectedUser] = useState(null);
  const [isModalVisible, setIsModalVisible] = useState(false);
  const [isCreateModalVisible, setIsCreateModalVisible] = useState(false);
  const [isAddressModalVisible, setIsAddressModalVisible] = useState(false);
  const [editingAddress, setEditingAddress] = useState(null);
  const [userAddresses, setUserAddresses] = useState([]);
  const [addressesLoading, setAddressesLoading] = useState(false);
  const [districts, setDistricts] = useState([]);
  const [addressCoordinates, setAddressCoordinates] = useState(null);
  const [pagination, setPagination] = useState({ page: 1, per_page: 20 });
  const responsive = useResponsive();
  const [createForm] = Form.useForm();
  const [addressForm] = Form.useForm();

  const queryClient = useQueryClient();

  // Fetch users
  const { data, isLoading } = useQuery(
    ['users', pagination, searchText, statusFilter, registrationMethodFilter],
    () => adminService.getUsers({
      page: pagination.page,
      per_page: pagination.per_page,
      search: searchText,
      status: statusFilter,
      registration_method: registrationMethodFilter
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
      onError: () => {
        message.error(t('ui.users.status_update_failed'));
      }
    }
  );

  // Create user mutation
  const createUserMutation = useMutation(
    (userData) => adminService.createUser(userData),
    {
      onSuccess: () => {
        message.success(t('ui.users.user_created_success', 'User created successfully'));
        setIsCreateModalVisible(false);
        createForm.resetFields();
        queryClient.invalidateQueries('users');
      },
      onError: (error) => {
        const errorMessage = error.response?.data?.message || t('ui.users.user_create_failed', 'Failed to create user');
        message.error(errorMessage);
      }
    }
  );

  // Create address mutation
  const createAddressMutation = useMutation(
    ({ userId, addressData }) => adminService.createUserAddress(userId, addressData),
    {
      onSuccess: () => {
        message.success(t('ui.users.address_created', 'Address created successfully'));
        setIsAddressModalVisible(false);
        addressForm.resetFields();
        if (selectedUser) {
          fetchUserAddresses(selectedUser.id);
        }
      },
      onError: (error) => {
        const errorMessage = error.response?.data?.message || t('ui.users.address_create_failed', 'Failed to create address');
        message.error(errorMessage);
      }
    }
  );

  // Update address mutation
  const updateAddressMutation = useMutation(
    ({ userId, addressId, addressData }) => adminService.updateUserAddress(userId, addressId, addressData),
    {
      onSuccess: () => {
        message.success(t('ui.users.address_updated', 'Address updated successfully'));
        setIsAddressModalVisible(false);
        setEditingAddress(null);
        addressForm.resetFields();
        if (selectedUser) {
          fetchUserAddresses(selectedUser.id);
        }
      },
      onError: (error) => {
        const errorMessage = error.response?.data?.message || t('ui.users.address_update_failed', 'Failed to update address');
        message.error(errorMessage);
      }
    }
  );

  // Delete address mutation
  const deleteAddressMutation = useMutation(
    ({ userId, addressId }) => adminService.deleteUserAddress(userId, addressId),
    {
      onSuccess: () => {
        message.success(t('ui.users.address_deleted', 'Address deleted successfully'));
        if (selectedUser) {
          fetchUserAddresses(selectedUser.id);
        }
      },
      onError: (error) => {
        const errorMessage = error.response?.data?.message || t('ui.users.address_delete_failed', 'Failed to delete address');
        message.error(errorMessage);
      }
    }
  );

  // Fetch user addresses
  const fetchUserAddresses = async (userId) => {
    setAddressesLoading(true);
    try {
      const response = await adminService.getUserAddresses(userId);
      setUserAddresses(response.data?.addresses || []);
    } catch (error) {
      message.error(t('ui.users.addresses_load_failed', 'Failed to load addresses'));
      setUserAddresses([]);
    } finally {
      setAddressesLoading(false);
    }
  };

  // Fetch districts for address form
  const fetchDistricts = async () => {
    try {
      const lang = localStorage.getItem('language') || 'en';
      const response = await api.get(`/addresses/districts?lang=${lang}`);
      if (response.data?.success && response.data?.data?.districts) {
        setDistricts(response.data.data.districts);
      }
    } catch (error) {
      console.error('Failed to load districts:', error);
    }
  };

  // Handle add address
  const handleAddAddress = () => {
    setEditingAddress(null);
    addressForm.resetFields();
    setAddressCoordinates(null);
    fetchDistricts();
    setIsAddressModalVisible(true);
  };

  // Handle edit address
  const handleEditAddress = (address) => {
    setEditingAddress(address);
    fetchDistricts();
    addressForm.setFieldsValue({
      title: address.title,
      full_address: address.full_address,
      district: address.district,
      street_address: address.street_address,
      floor_number: address.floor_number,
      apartment_number: address.apartment_number,
      landmark: address.landmark,
      delivery_instructions: address.delivery_instructions,
      is_default: address.is_default,
      is_business: address.is_business
    });
    // Set coordinates if available
    if (address.latitude && address.longitude) {
      setAddressCoordinates({
        latitude: address.latitude,
        longitude: address.longitude
      });
    } else {
      setAddressCoordinates(null);
    }
    setIsAddressModalVisible(true);
  };

  // Handle map coordinate change
  const handleMapCoordinateChange = useCallback((coords) => {
    setAddressCoordinates(coords);
  }, []);

  // Handle address found from map (reverse geocode result)
  const handleMapAddressFound = useCallback((addressData) => {
    if (addressData.formatted_address) {
      addressForm.setFieldsValue({
        full_address: addressData.formatted_address
      });
    }
    if (addressData.district) {
      // Try to find matching district key
      const matchedDistrict = districts.find(d =>
        d.name.toLowerCase().includes(addressData.district.toLowerCase()) ||
        addressData.district.toLowerCase().includes(d.name.toLowerCase())
      );
      if (matchedDistrict) {
        addressForm.setFieldsValue({
          district: matchedDistrict.key
        });
      }
    }
  }, [addressForm, districts]);

  // Handle delete address
  const handleDeleteAddress = (address) => {
    Modal.confirm({
      title: t('ui.users.delete_address_confirm', 'Delete this address?'),
      content: address.full_address,
      okText: t('ui.users.delete', 'Delete'),
      okType: 'danger',
      cancelText: t('ui.common.cancel', 'Cancel'),
      onOk: () => {
        deleteAddressMutation.mutate({
          userId: selectedUser.id,
          addressId: address.id
        });
      }
    });
  };

  // Handle address form submit
  const handleAddressSubmit = (values) => {
    // Include coordinates from map picker
    const addressData = {
      ...values,
      latitude: addressCoordinates?.latitude || null,
      longitude: addressCoordinates?.longitude || null
    };

    if (editingAddress) {
      updateAddressMutation.mutate({
        userId: selectedUser.id,
        addressId: editingAddress.id,
        addressData
      });
    } else {
      createAddressMutation.mutate({
        userId: selectedUser.id,
        addressData
      });
    }
  };

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
      render: (text, record) => {
        // Registration method icon and color
        const methodConfig = {
          phone: { icon: <PhoneOutlined />, color: 'green', label: t('ui.users.reg_method_phone', 'Phone') },
          email: { icon: <MailOutlined />, color: 'purple', label: t('ui.users.reg_method_email', 'Email') },
          telegram: { icon: <MessageOutlined />, color: 'blue', label: t('ui.users.reg_method_telegram', 'Telegram') }
        };
        const method = record.registration_method || 'email';
        const config = methodConfig[method] || methodConfig.email;

        return (
          <Space direction="vertical" size={4}>
            {record.phone && (
              <div style={{ fontSize: '14px' }}>{record.phone}</div>
            )}
            <div>
              <Tag
                color={config.color}
                size="small"
                icon={config.icon}
              >
                {config.label}
              </Tag>
            </div>
          </Space>
        );
      }
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
    setUserAddresses([]);
    fetchUserAddresses(user.id);
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

  const handleRegistrationMethodFilter = (value) => {
    setRegistrationMethodFilter(value);
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
              <Select
                placeholder={t('ui.users.filter_by_registration', 'Registration')}
                allowClear
                onChange={handleRegistrationMethodFilter}
                style={{
                  width: '150px',
                  minHeight: responsive.isTouchDevice ? '40px' : '32px'
                }}
              >
                <Option value="email">
                  <Space><MailOutlined />{t('ui.users.reg_method_email', 'Email')}</Space>
                </Option>
                <Option value="phone">
                  <Space><PhoneOutlined />{t('ui.users.reg_method_phone', 'Phone')}</Space>
                </Option>
                <Option value="telegram">
                  <Space><MessageOutlined />{t('ui.users.reg_method_telegram', 'Telegram')}</Space>
                </Option>
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
                onClick={() => setIsCreateModalVisible(true)}
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
                    <strong>{t('ui.users.registration_method', 'Registration Method')}:</strong>
                    <Tag
                      color={
                        selectedUser.registration_method === 'phone' ? 'green' :
                        selectedUser.registration_method === 'telegram' ? 'blue' : 'purple'
                      }
                      style={{ marginLeft: 8 }}
                      icon={
                        selectedUser.registration_method === 'phone' ? <PhoneOutlined /> :
                        selectedUser.registration_method === 'telegram' ? <MessageOutlined /> : <MailOutlined />
                      }
                      size="small"
                    >
                      {selectedUser.registration_method === 'phone' ? t('ui.users.reg_method_phone', 'Phone') :
                       selectedUser.registration_method === 'telegram' ? t('ui.users.reg_method_telegram', 'Telegram') :
                       t('ui.users.reg_method_email', 'Email')}
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
            <Card title={t('ui.users.activity_information')} size="small" style={{ marginBottom: 12 }}>
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

            {/* Addresses */}
            <Card
              title={
                <Space>
                  <EnvironmentOutlined />
                  {t('ui.users.addresses', 'Addresses')}
                </Space>
              }
              size="small"
              extra={
                <Button
                  type="primary"
                  size="small"
                  icon={<PlusOutlined />}
                  onClick={handleAddAddress}
                >
                  {t('ui.users.add_address', 'Add Address')}
                </Button>
              }
            >
              {addressesLoading ? (
                <div style={{ textAlign: 'center', padding: '20px' }}>
                  {t('ui.common.loading', 'Loading...')}
                </div>
              ) : userAddresses.length === 0 ? (
                <div style={{
                  textAlign: 'center',
                  padding: '20px',
                  color: '#999'
                }}>
                  {t('ui.users.no_addresses_yet', 'No addresses saved yet')}
                </div>
              ) : (
                <div>
                  {userAddresses.map((address, index) => (
                    <div
                      key={address.id}
                      style={{
                        padding: '12px',
                        background: index % 2 === 0 ? '#fafafa' : '#fff',
                        borderRadius: 6,
                        marginBottom: index < userAddresses.length - 1 ? 8 : 0,
                        border: '1px solid #f0f0f0'
                      }}
                    >
                      <Row justify="space-between" align="top">
                        <Col flex="1">
                          <Space direction="vertical" size={4} style={{ width: '100%' }}>
                            <div style={{ fontWeight: 600 }}>
                              {address.title || t('ui.users.title_other', 'Other')}
                              {address.is_default && (
                                <Tag color="green" size="small" style={{ marginLeft: 8 }}>
                                  {t('ui.users.default', 'Default')}
                                </Tag>
                              )}
                              {address.is_business && (
                                <Tag color="blue" size="small" style={{ marginLeft: 4 }}>
                                  {t('ui.users.is_business', 'Business')}
                                </Tag>
                              )}
                            </div>
                            <div style={{ color: '#666', fontSize: '13px' }}>
                              {address.full_address}
                            </div>
                            {(address.district || address.apartment_number || address.floor_number) && (
                              <div style={{ color: '#888', fontSize: '12px' }}>
                                {address.district && <span>{address.district}</span>}
                                {address.floor_number && <span> • {t('ui.users.floor', 'Floor')}: {address.floor_number}</span>}
                                {address.apartment_number && <span> • {t('ui.users.apartment', 'Apt')}: {address.apartment_number}</span>}
                              </div>
                            )}
                            {address.delivery_instructions && (
                              <div style={{ color: '#1890ff', fontSize: '12px', fontStyle: 'italic' }}>
                                📝 {address.delivery_instructions}
                              </div>
                            )}
                          </Space>
                        </Col>
                        <Col>
                          <Space size={4}>
                            <Button
                              type="text"
                              size="small"
                              icon={<EditOutlined />}
                              onClick={() => handleEditAddress(address)}
                            />
                            <Button
                              type="text"
                              size="small"
                              danger
                              icon={<DeleteOutlined />}
                              onClick={() => handleDeleteAddress(address)}
                            />
                          </Space>
                        </Col>
                      </Row>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          </div>
        )}
      </Modal>

      {/* Address Add/Edit Modal */}
      <Modal
        title={editingAddress
          ? t('ui.users.edit_address', 'Edit Address')
          : t('ui.users.add_address', 'Add Address')
        }
        open={isAddressModalVisible}
        onCancel={() => {
          setIsAddressModalVisible(false);
          setEditingAddress(null);
          setAddressCoordinates(null);
          addressForm.resetFields();
        }}
        onOk={() => addressForm.submit()}
        confirmLoading={createAddressMutation.isLoading || updateAddressMutation.isLoading}
        okText={editingAddress ? t('ui.common.save', 'Save') : t('ui.users.add', 'Add')}
        cancelText={t('ui.common.cancel', 'Cancel')}
        width={responsive.isMobileDevice ? '95%' : 750}
      >
        <Form
          form={addressForm}
          layout="vertical"
          onFinish={handleAddressSubmit}
          style={{ marginTop: 16 }}
        >
          {/* Quick Title Selection */}
          <div style={{ marginBottom: 8 }}>
            <label style={{ display: 'block', marginBottom: 4 }}>
              {t('ui.users.quick_select', 'Quick Select')}:
            </label>
            <Space wrap>
              <Button
                size="small"
                onClick={() => addressForm.setFieldsValue({ title: t('ui.users.title_home', 'Home') })}
              >
                🏠 {t('ui.users.title_home', 'Home')}
              </Button>
              <Button
                size="small"
                onClick={() => addressForm.setFieldsValue({ title: t('ui.users.title_work', 'Work') })}
              >
                💼 {t('ui.users.title_work', 'Work')}
              </Button>
              <Button
                size="small"
                onClick={() => addressForm.setFieldsValue({ title: t('ui.users.title_other', 'Other') })}
              >
                📍 {t('ui.users.title_other', 'Other')}
              </Button>
            </Space>
          </div>

          <Form.Item
            name="title"
            label={t('ui.users.address_title', 'Address Title')}
          >
            <Input placeholder={t('ui.users.address_title_placeholder', 'e.g., Home, Office, etc.')} />
          </Form.Item>

          <Divider style={{ margin: '12px 0' }}>{t('ui.users.location_details', 'Location Details')}</Divider>

          {/* Map Picker for Location Selection */}
          <div style={{ marginBottom: 16 }}>
            <label style={{ display: 'block', marginBottom: 8, fontWeight: 500 }}>
              {t('ui.users.select_location_on_map', 'Select Location on Map')}
            </label>
            <AddressMapPicker
              value={addressCoordinates}
              onChange={handleMapCoordinateChange}
              onAddressFound={handleMapAddressFound}
              height={250}
            />
          </div>

          <Form.Item
            name="full_address"
            label={t('ui.users.full_address', 'Full Address')}
            rules={[
              { required: true, message: t('ui.users.address_required', 'Address is required') }
            ]}
          >
            <Input.TextArea
              rows={2}
              placeholder={t('ui.users.full_address_placeholder', 'Enter full delivery address...')}
            />
          </Form.Item>

          <Row gutter={16}>
            <Col xs={24} sm={12}>
              <Form.Item
                name="district"
                label={t('ui.users.district', 'District')}
              >
                <Select
                  placeholder={t('ui.users.select_district', 'Select district')}
                  allowClear
                  showSearch
                  optionFilterProp="children"
                >
                  {districts.map(d => (
                    <Option key={d.key} value={d.key}>{d.name}</Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
            <Col xs={24} sm={12}>
              <Form.Item
                name="street_address"
                label={t('ui.users.street', 'Street')}
              >
                <Input placeholder={t('ui.users.street_placeholder', 'Street name')} />
              </Form.Item>
            </Col>
          </Row>

          <Divider style={{ margin: '12px 0' }}>{t('ui.users.building_details', 'Building Details')}</Divider>

          <Row gutter={16}>
            <Col xs={12} sm={6}>
              <Form.Item
                name="floor_number"
                label={t('ui.users.floor', 'Floor')}
              >
                <Input placeholder={t('ui.users.floor_placeholder', '#')} />
              </Form.Item>
            </Col>
            <Col xs={12} sm={6}>
              <Form.Item
                name="apartment_number"
                label={t('ui.users.apartment', 'Apt/Unit')}
              >
                <Input placeholder={t('ui.users.apartment_placeholder', '#')} />
              </Form.Item>
            </Col>
            <Col xs={24} sm={12}>
              <Form.Item
                name="landmark"
                label={t('ui.users.landmark', 'Landmark')}
              >
                <Input placeholder={t('ui.users.landmark_placeholder', 'Nearby landmark')} />
              </Form.Item>
            </Col>
          </Row>

          <Divider style={{ margin: '12px 0' }}>{t('ui.users.delivery_info', 'Delivery Information')}</Divider>

          <Form.Item
            name="delivery_instructions"
            label={t('ui.users.delivery_instructions', 'Delivery Instructions')}
          >
            <Input.TextArea
              rows={2}
              placeholder={t('ui.users.delivery_instructions_placeholder', 'Door code, ring doorbell, leave at door, etc.')}
            />
          </Form.Item>

          <Row gutter={16}>
            <Col xs={12}>
              <Form.Item
                name="is_default"
                valuePropName="checked"
              >
                <Checkbox>
                  {t('ui.users.set_as_default', 'Set as default address')}
                </Checkbox>
              </Form.Item>
            </Col>
            <Col xs={12}>
              <Form.Item
                name="is_business"
                valuePropName="checked"
              >
                <Checkbox>
                  {t('ui.users.is_business', 'Business address')}
                </Checkbox>
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>

      {/* Create User Modal */}
      <Modal
        title={t('ui.users.create_new_user', 'Create New User')}
        open={isCreateModalVisible}
        onCancel={() => {
          setIsCreateModalVisible(false);
          createForm.resetFields();
        }}
        onOk={() => createForm.submit()}
        confirmLoading={createUserMutation.isLoading}
        okText={t('ui.users.create', 'Create')}
        cancelText={t('ui.common.cancel', 'Cancel')}
        width={responsive.isMobileDevice ? '95%' : 500}
        style={{
          maxWidth: responsive.isMobileDevice ? 'none' : 500,
          top: responsive.isMobileDevice ? 20 : 100
        }}
      >
        <Form
          form={createForm}
          layout="vertical"
          onFinish={(values) => createUserMutation.mutate(values)}
          style={{ marginTop: 16 }}
        >
          <Row gutter={16}>
            <Col xs={24} sm={12}>
              <Form.Item
                name="first_name"
                label={t('ui.users.first_name', 'First Name')}
                rules={[
                  { required: true, message: t('ui.users.first_name_required', 'First name is required') }
                ]}
              >
                <Input placeholder={t('ui.users.enter_first_name', 'Enter first name')} />
              </Form.Item>
            </Col>
            <Col xs={24} sm={12}>
              <Form.Item
                name="last_name"
                label={t('ui.users.last_name', 'Last Name')}
              >
                <Input placeholder={t('ui.users.enter_last_name', 'Enter last name')} />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item
            name="phone"
            label={t('ui.users.phone', 'Phone Number')}
            rules={[
              { required: true, message: t('ui.users.phone_required', 'Phone number is required') },
              {
                pattern: /^\+?[1-9]\d{8,14}$/,
                message: t('ui.users.invalid_phone', 'Please enter a valid phone number (e.g., +998901234567)')
              }
            ]}
          >
            <Input placeholder="+998901234567" />
          </Form.Item>

          <Form.Item
            name="email"
            label={t('ui.users.email', 'Email')}
            rules={[
              { type: 'email', message: t('ui.users.invalid_email', 'Please enter a valid email address') }
            ]}
          >
            <Input placeholder={t('ui.users.enter_email_optional', 'Enter email (optional)')} />
          </Form.Item>

          <Form.Item
            name="notes"
            label={t('ui.users.admin_notes', 'Admin Notes')}
          >
            <Input.TextArea
              rows={3}
              placeholder={t('ui.users.notes_placeholder', 'Add any notes about this user...')}
            />
          </Form.Item>

          <div style={{
            background: '#f6ffed',
            border: '1px solid #b7eb8f',
            borderRadius: 6,
            padding: 12,
            marginBottom: 8
          }}>
            <strong>{t('ui.users.note', 'Note')}:</strong>{' '}
            {t('ui.users.admin_created_user_note',
              'Users created here are for phone orders only. They cannot login to the customer portal.'
            )}
          </div>
        </Form>
      </Modal>
    </div>
  );
};

export default Users;