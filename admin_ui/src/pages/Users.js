import React, { useState, useCallback } from 'react';
import dayjs from 'dayjs';
import { DEFAULT_PAGE_SIZE } from '../utils/constants';
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
  Switch,
  message,
  Row,
  Col,
  Checkbox,
  Divider,
  Tooltip,
  DatePicker
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
  MailOutlined,
  LockOutlined,
  UnlockOutlined
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import adminService from '../services/adminService';
import staffService from '../services/staffService';
import api from '../services/api';
import useResponsive from '../hooks/useResponsive';
import AddressMapPicker from '../components/AddressMapPicker';
import { formatLocalDate, formatLocaleDateTime } from '../utils/dateUtils';
import exportUtils from '../utils/exportUtils';

const { Option } = Select;
const USER_TYPE_OPTIONS = [
  { value: 'individual', labelKey: 'ui.users.user_type_individual', fallback: 'Individual' },
  { value: 'entity', labelKey: 'ui.users.user_type_entity', fallback: 'Entity' }
];

const ENTITY_SUBTYPE_OPTIONS = [
  { value: 'workplace', labelKey: 'ui.users.entity_subtype_workplace', fallback: 'Workplace' },
  { value: 'grocery_store', labelKey: 'ui.users.entity_subtype_grocery_store', fallback: 'Grocery Store' }
];

const getEntitySubtypeMeta = (t, subtype) => {
  if (subtype === 'grocery_store') {
    return { color: 'orange', label: t('ui.users.entity_subtype_grocery_store', 'Grocery Store') };
  }
  if (subtype === 'workplace') {
    return { color: 'blue', label: t('ui.users.entity_subtype_workplace', 'Workplace') };
  }
  return null;
};

const getUserTypeMeta = (t, userType) => {
  if (userType === 'entity') {
    return { color: 'gold', label: t('ui.users.user_type_entity', 'Entity') };
  }
  if (userType === 'staff') {
    return { color: 'blue', label: t('ui.users.user_type_staff', 'Staff') };
  }
  return { color: 'default', label: t('ui.users.user_type_individual', 'Individual') };
};

const Users = () => {
  // Load users namespace for ui.users.* keys
  const { t } = useTranslation('users');
  const navigate = useNavigate();
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [registrationMethodFilter, setRegistrationMethodFilter] = useState('');
  const [selectedUser, setSelectedUser] = useState(null);
  const [editingUser, setEditingUser] = useState(null);
  const [isModalVisible, setIsModalVisible] = useState(false);
  const [isCreateModalVisible, setIsCreateModalVisible] = useState(false);
  const [isAddressModalVisible, setIsAddressModalVisible] = useState(false);
  const [editingAddress, setEditingAddress] = useState(null);
  const [userAddresses, setUserAddresses] = useState([]);
  const [addressesLoading, setAddressesLoading] = useState(false);
  const [notificationSettings, setNotificationSettings] = useState(null);
  const [notificationSettingsLoading, setNotificationSettingsLoading] = useState(false);
  const [notificationSettingsError, setNotificationSettingsError] = useState('');
  const [userCodStatement, setUserCodStatement] = useState(null);
  const [userCodStatementLoading, setUserCodStatementLoading] = useState(false);
  const [userPrepaymentHistory, setUserPrepaymentHistory] = useState(null);
  const [userPrepaymentHistoryLoading, setUserPrepaymentHistoryLoading] = useState(false);
  const [userCart, setUserCart] = useState(null);
  const [userCartLoading, setUserCartLoading] = useState(false);
  const [isNotificationReasonModalVisible, setIsNotificationReasonModalVisible] = useState(false);
  const [pendingNotificationToggle, setPendingNotificationToggle] = useState(null);
  const [notificationReason, setNotificationReason] = useState('');
  const [districts, setDistricts] = useState([]);
  const [addressCoordinates, setAddressCoordinates] = useState(null);
  const [pagination, setPagination] = useState({ page: 1, per_page: DEFAULT_PAGE_SIZE });
  const responsive = useResponsive();
  const [createForm] = Form.useForm();
  const [addressForm] = Form.useForm();
  const selectedUserType = Form.useWatch('user_type', createForm);
  const selectedEntitySubtype = Form.useWatch('entity_subtype', createForm);
  const isEditingStaffUser = editingUser?.user_type === 'staff';

  const queryClient = useQueryClient();
  const selectedUserTypeMeta = getUserTypeMeta(t, selectedUser?.user_type);

  // Fetch users
  const { data, isLoading } = useQuery({
    queryKey: ['users', pagination, searchText, statusFilter, registrationMethodFilter],

    queryFn: () => adminService.getUsers({
      page: pagination.page,
      per_page: pagination.per_page,
      search: searchText,
      status: statusFilter,
      registration_method: registrationMethodFilter
    }),

    placeholderData: keepPreviousData,
  });

  const handleExportUsers = async () => {
    const result = await exportUtils.exportUsers({
      search: searchText,
      status: statusFilter,
      registration_method: registrationMethodFilter
    });
    if (!result.success) {
      message.error(result.message);
    }
  };

  // Update user status mutation
  const updateUserMutation = useMutation({
    mutationFn: ({ userId, status, reason }) => adminService.updateUserStatus(userId, status, reason),

    onSuccess: () => {
      message.success(t('ui.users.status_updated_success'));
      queryClient.invalidateQueries({
        queryKey: ['users'],
      });
    },

    onError: () => {
      message.error(t('ui.users.status_update_failed'));
    },
  });

  // Create user mutation
  const createUserMutation = useMutation({
    mutationFn: (userData) => adminService.createUser(userData),

    onSuccess: () => {
      message.success(t('ui.users.user_created_success', 'User created successfully'));
      setIsCreateModalVisible(false);
      createForm.resetFields();
      queryClient.invalidateQueries({
        queryKey: ['users'],
      });
    },

    onError: (error) => {
      const errorMessage = error.response?.data?.message || t('ui.users.user_create_failed', 'Failed to create user');
      message.error(errorMessage);
    },
  });

  const editUserMutation = useMutation({
    mutationFn: ({ userId, userData }) => adminService.updateUser(userId, userData),

    onSuccess: (response) => {
      const updatedUser = response?.data?.user || null;
      message.success(t('ui.users.user_updated_success', 'User updated successfully'));
      setIsCreateModalVisible(false);
      setEditingUser(null);
      createForm.resetFields();
      queryClient.invalidateQueries({
        queryKey: ['users'],
      });
      if (updatedUser && selectedUser?.id === updatedUser.id) {
        setSelectedUser(updatedUser);
      }
    },

    onError: (error) => {
      const errorMessage = error.response?.data?.message || t('ui.users.user_update_failed', 'Failed to update user');
      message.error(errorMessage);
    },
  });

  // Create address mutation
  const createAddressMutation = useMutation({
    mutationFn: ({ userId, addressData }) => adminService.createUserAddress(userId, addressData),

    onSuccess: () => {
      message.success(t('ui.users.address_created', 'Address created successfully'));
      setIsAddressModalVisible(false);
      addressForm.resetFields();
      if (selectedUser) {
        // eslint-disable-next-line no-use-before-define
        fetchUserAddresses(selectedUser.id);
      }
    },

    onError: (error) => {
      const errorMessage = error.response?.data?.message || t('ui.users.address_create_failed', 'Failed to create address');
      message.error(errorMessage);
    },
  });

  // Update address mutation
  const updateAddressMutation = useMutation({
    mutationFn: ({ userId, addressId, addressData }) => adminService.updateUserAddress(userId, addressId, addressData),

    onSuccess: () => {
      message.success(t('ui.users.address_updated', 'Address updated successfully'));
      setIsAddressModalVisible(false);
      setEditingAddress(null);
      addressForm.resetFields();
      if (selectedUser) {
        // eslint-disable-next-line no-use-before-define
        fetchUserAddresses(selectedUser.id);
      }
    },

    onError: (error) => {
      const errorMessage = error.response?.data?.message || t('ui.users.address_update_failed', 'Failed to update address');
      message.error(errorMessage);
    },
  });

  // Delete address mutation
  const deleteAddressMutation = useMutation({
    mutationFn: ({ userId, addressId }) => adminService.deleteUserAddress(userId, addressId),

    onSuccess: () => {
      message.success(t('ui.users.address_deleted', 'Address deleted successfully'));
      if (selectedUser) {
        // eslint-disable-next-line no-use-before-define
        fetchUserAddresses(selectedUser.id);
      }
    },

    onError: (error) => {
      const errorMessage = error.response?.data?.message || t('ui.users.address_delete_failed', 'Failed to delete address');
      message.error(errorMessage);
    },
  });

  // Unlock user account mutation
  const unlockUserMutation = useMutation({
    mutationFn: (userId) => adminService.unlockUserAccount(userId),

    onSuccess: () => {
      message.success(t('ui.users.account_unlocked', 'Account unlocked successfully'));
      queryClient.invalidateQueries({
        queryKey: ['users'],
      });
    },

    onError: (error) => {
      const errorMessage = error.response?.data?.message || t('ui.users.unlock_failed', 'Failed to unlock account');
      message.error(errorMessage);
    },
  });

  const updateUserNotificationSettingsMutation = useMutation({
    mutationFn: ({ userId, enabled, reason }) => adminService.updateUserNotificationSettings(userId, {
      delivery_telegram_status_updates_enabled: enabled,
      reason
    }),

    onSuccess: (response) => {
      const settings = response?.data?.notification_settings || response?.notification_settings || null;
      if (settings) {
        setNotificationSettings(settings);
      }
      message.success(
        response?.message || t('ui.users.notification_settings_updated', 'Notification settings updated')
      );
      setIsNotificationReasonModalVisible(false);
      setPendingNotificationToggle(null);
      setNotificationReason('');
    },

    onError: (error) => {
      const errorMessage = error.response?.data?.message || t('ui.users.notification_settings_update_failed', 'Failed to update notification settings');
      message.error(errorMessage);
    },
  });

  // Handle unlock user
  const handleUnlockUser = (userId) => {
    Modal.confirm({
      title: t('ui.users.unlock_account_title', 'Unlock User Account?'),
      content: t('ui.users.unlock_account_confirm', 'This will clear the account lockout and allow the user to login again.'),
      okText: t('ui.users.unlock', 'Unlock'),
      cancelText: t('ui.common.cancel', 'Cancel'),
      onOk: () => {
        unlockUserMutation.mutate(userId);
      }
    });
  };

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

  const fetchUserNotificationSettings = async (userId) => {
    setNotificationSettingsLoading(true);
    setNotificationSettingsError('');
    try {
      const response = await adminService.getUserNotificationSettings(userId);
      const settings = response?.data?.notification_settings || response?.notification_settings || null;
      setNotificationSettings(settings);
      if (!settings) {
        setNotificationSettingsError(
          t('ui.users.notification_settings_load_failed', 'Failed to load notification settings')
        );
      }
    } catch (error) {
      setNotificationSettings(null);
      setNotificationSettingsError(
        error.response?.data?.message || t('ui.users.notification_settings_load_failed', 'Failed to load notification settings')
      );
    } finally {
      setNotificationSettingsLoading(false);
    }
  };

  const fetchUserCodStatement = async (user) => {
    if (!user || user.role !== 'customer') {
      setUserCodStatement(null);
      return;
    }

    setUserCodStatementLoading(true);
    try {
      const response = await staffService.getCustomerCodStatement(user.id);
      setUserCodStatement(response?.data?.data || null);
    } catch (_error) {
      setUserCodStatement(null);
    } finally {
      setUserCodStatementLoading(false);
    }
  };

  // Fetch the customer's most recent COD prepayment events + allocations so we
  // can preview the ledger inline in the user detail modal. The full ledger
  // lives on /staff/prepayments?customer_id=<id>.
  const fetchUserPrepaymentHistory = async (user) => {
    if (!user || user.role !== 'customer') {
      setUserPrepaymentHistory(null);
      return;
    }

    setUserPrepaymentHistoryLoading(true);
    try {
      const response = await staffService.getCustomerPrepaymentHistory(user.id, {
        include_voided: 1,
        include_fully_applied: 1,
        limit: 20,
      });
      setUserPrepaymentHistory(response?.data?.data || null);
    } catch (_error) {
      setUserPrepaymentHistory(null);
    } finally {
      setUserPrepaymentHistoryLoading(false);
    }
  };

  const fetchUserCart = async (user) => {
    if (!user || user.role !== 'customer') {
      setUserCart(null);
      return;
    }

    setUserCartLoading(true);
    try {
      const response = await adminService.getUserCart(user.id);
      setUserCart(response?.data || null);
    } catch (_error) {
      setUserCart(null);
    } finally {
      setUserCartLoading(false);
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
            <Tag color={getUserTypeMeta(t, record.user_type).color} size="small">
              {getUserTypeMeta(t, record.user_type).label}
            </Tag>
            {(() => {
              const subtypeMeta = getEntitySubtypeMeta(t, record.entity_subtype);
              return subtypeMeta ? (
                <Tag color={subtypeMeta.color} size="small">
                  {subtypeMeta.label}
                </Tag>
              ) : null;
            })()}
            {record.user_type === 'entity' && !record.entity_subtype && (
              <Tag color="warning" size="small" title={t('ui.users.entity_subtype_unassigned_note', 'Subtype unassigned')}>
                {t('ui.users.entity_subtype_unassigned', 'Subtype unassigned')}
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
        // eslint-disable-next-line security/detect-object-injection
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
        const isLocked = record.account_locked_until && new Date(record.account_locked_until) > new Date();
        return (
          <Space direction="vertical" size={2}>
            {/* eslint-disable-next-line security/detect-object-injection */}
            <Tag color={colors[status]} size="small">
              {t(`ui.users.status_${status}`)}
            </Tag>
            {isLocked && (
              <Tag color="error" size="small" icon={<LockOutlined />}>
                {t('ui.users.locked', 'Locked')}
              </Tag>
            )}
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
            {t('ui.users.created')}: {formatLocalDate(record.created_at)}
          </div>
          <div style={{ fontSize: '11px' }}>
            {t('ui.users.login')}: {record.last_login ? formatLocalDate(record.last_login) : t('ui.users.never')}
          </div>
          {record.last_bot_interaction && (
            <div style={{ fontSize: '11px', color: '#1890ff' }}>
              {t('ui.users.bot')}: {formatLocalDate(record.last_bot_interaction)}
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
                // eslint-disable-next-line no-use-before-define
                onClick: () => handleViewUser(record)
              },
              {
                key: 'edit',
                label: t('ui.users.edit_user', 'Edit User'),
                // eslint-disable-next-line no-use-before-define
                onClick: () => handleEditUser(record)
              },
              {
                key: 'activate',
                label: t('ui.users.activate'),
                disabled: record.status === 'active',
                // eslint-disable-next-line no-use-before-define
                onClick: () => handleStatusChange(record.id, 'active')
              },
              {
                key: 'suspend',
                label: t('ui.users.suspend'),
                disabled: record.status === 'suspended',
                // eslint-disable-next-line no-use-before-define
                onClick: () => handleStatusChange(record.id, 'suspended')
              },
              {
                key: 'ban',
                label: t('ui.users.ban'),
                danger: true,
                disabled: record.status === 'banned',
                // eslint-disable-next-line no-use-before-define
                onClick: () => handleStatusChange(record.id, 'banned')
              },
              // Show unlock option if account is locked
              ...(record.account_locked_until && new Date(record.account_locked_until) > new Date() ? [{
                type: 'divider'
              }, {
                key: 'unlock',
                label: t('ui.users.unlock_account', 'Unlock Account'),
                icon: <UnlockOutlined />,
                onClick: () => handleUnlockUser(record.id)
              }] : [])
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
    setNotificationSettings(null);
    setNotificationSettingsError('');
    setUserCodStatement(null);
    setUserPrepaymentHistory(null);
    setUserCart(null);
    fetchUserAddresses(user.id);
    fetchUserNotificationSettings(user.id);
    fetchUserCodStatement(user);
    fetchUserPrepaymentHistory(user);
    fetchUserCart(user);
  };

  const handleEditUser = (user) => {
    setEditingUser(user);
    setIsCreateModalVisible(true);
    createForm.setFieldsValue({
      first_name: user.first_name || '',
      last_name: user.last_name || '',
      phone: user.phone || '',
      email: user.email || '',
      user_type: user.user_type || 'individual',
      entity_subtype: user.entity_subtype || undefined,
      company_name: user.company_name || '',
      tax_id: user.tax_id || '',
      date_of_birth: user.date_of_birth ? dayjs(user.date_of_birth) : null,
      cod_debt_check_exempt: !!user.cod_debt_check_exempt
    });
  };

  const handleCreateOrEditSubmit = (values) => {
    const payload = {
      ...values,
      user_type: values.user_type || (editingUser?.user_type || 'individual'),
      date_of_birth: values.date_of_birth
        ? dayjs(values.date_of_birth).format('YYYY-MM-DD')
        : null
    };

    if (payload.user_type !== 'entity') {
      payload.company_name = '';
      payload.tax_id = '';
      payload.entity_subtype = null;
    } else if (!payload.entity_subtype) {
      // Required for new entity users; for edits we send null only when user
      // explicitly cleared it. Empty/undefined on a new user falls through to
      // the backend which rejects it. Keep payload pass-through.
      payload.entity_subtype = payload.entity_subtype || null;
    }

    if (editingUser) {
      editUserMutation.mutate({ userId: editingUser.id, userData: payload });
      return;
    }

    createUserMutation.mutate(payload);
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

  const handleNotificationToggleRequest = (enabled) => {
    setPendingNotificationToggle(enabled);
    setNotificationReason('');
    setIsNotificationReasonModalVisible(true);
  };

  const handleNotificationSettingsUpdateConfirm = () => {
    if (!selectedUser) {
      return;
    }
    const reason = notificationReason.trim();
    if (!reason) {
      message.error(
        t('ui.users.notification_change_reason_required', 'Reason is required')
      );
      return;
    }

    updateUserNotificationSettingsMutation.mutate({
      userId: selectedUser.id,
      enabled: Boolean(pendingNotificationToggle),
      reason
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
                onClick={handleExportUsers}
                style={{
                  minHeight: responsive.isTouchDevice ? '40px' : '32px'
                }}
              >
                {t('ui.users.export')}
              </Button>
              <Button
                type="primary"
                icon={<PlusOutlined />}
                onClick={() => {
                  setEditingUser(null);
                  createForm.resetFields();
                  createForm.setFieldsValue({ user_type: 'individual' });
                  setIsCreateModalVisible(true);
                }}
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
        onCancel={() => {
          setIsModalVisible(false);
          setNotificationSettings(null);
          setNotificationSettingsError('');
          setIsNotificationReasonModalVisible(false);
          setPendingNotificationToggle(null);
          setNotificationReason('');
        }}
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
                  {(selectedUser.company_name || selectedUser.user_type || selectedUser.tax_id) && (
                    <>
                      <div style={{ marginBottom: 8 }}>
                        <strong>{t('ui.users.company_name', 'Company Name')}:</strong> {selectedUser.company_name || t('ui.users.na')}
                      </div>
                      <div style={{ marginBottom: 8 }}>
                        <strong>{t('ui.users.user_type', 'User Type')}:</strong>
                        <Tag color={selectedUserTypeMeta.color} style={{ marginLeft: 8 }}>
                          {selectedUserTypeMeta.label}
                        </Tag>
                      </div>
                      <div style={{ marginBottom: 8 }}>
                        <strong>{t('ui.users.tax_id', 'Tax ID')}:</strong> {selectedUser.tax_id || t('ui.users.na')}
                      </div>
                    </>
                  )}
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

            {selectedUser.role === 'customer' && (
              <Card
                title={t('ui.users.cod_statement', 'COD Statement')}
                size="small"
                style={{ marginBottom: 12 }}
              >
                {userCodStatementLoading ? (
                  <div style={{ textAlign: 'center', padding: '12px 0' }}>
                    {t('ui.common.loading', 'Loading...')}
                  </div>
                ) : userCodStatement ? (
                  <>
                    <Row gutter={[16, 12]} style={{ marginBottom: 12 }}>
                      <Col xs={24} sm={12}>
                        <strong>{t('ui.users.active_cod_debt_count', 'Active COD debts')}:</strong>{' '}
                        {userCodStatement.active_cod_debt_count || 0}
                      </Col>
                      <Col xs={24} sm={12}>
                        <strong>{t('ui.users.cod_restricted', 'COD restricted')}:</strong>{' '}
                        <Tag color={userCodStatement.cod_restricted ? 'red' : 'green'}>
                          {userCodStatement.cod_restricted ? t('ui.common.yes', 'Yes') : t('ui.common.no', 'No')}
                        </Tag>
                      </Col>
                    </Row>

                    {(userCodStatement.items && userCodStatement.items.length > 0) && (
                      <Row gutter={[16, 12]} style={{ marginBottom: 12 }}>
                        <Col xs={24} sm={12} md={6}>
                          <strong>{t('ui.users.gross_outstanding', 'Gross outstanding')}:</strong>{' '}
                          {(
                            userCodStatement.gross_outstanding_amount
                            ?? userCodStatement.total_outstanding_amount
                            ?? 0
                          ).toLocaleString()} UZS
                        </Col>
                        <Col xs={24} sm={12} md={6}>
                          <strong>{t('ui.users.reserved_from_prepayment', 'Reserved from prepayment')}:</strong>{' '}
                          {(userCodStatement.reserved_prepayment_total ?? 0).toLocaleString()} UZS
                        </Col>
                        <Col xs={24} sm={12} md={6}>
                          <strong>{t('ui.users.net_outstanding', 'Net outstanding')}:</strong>{' '}
                          <strong style={{ color: '#cf1322' }}>
                            {(userCodStatement.net_outstanding_amount ?? 0).toLocaleString()} UZS
                          </strong>
                        </Col>
                        <Col xs={24} sm={12} md={6}>
                          <strong>{t('ui.users.unreserved_prepayment_balance', 'Prepayment balance (unreserved)')}:</strong>{' '}
                          {(() => {
                            const unreserved = userCodStatement.unreserved_prepayment_balance
                              ?? userCodStatement.available_prepayment_balance
                              ?? 0;
                            return unreserved > 0 ? (
                              <Tag color="green">{unreserved.toLocaleString()} UZS</Tag>
                            ) : (
                              <span>{unreserved.toLocaleString()} UZS</span>
                            );
                          })()}
                        </Col>
                      </Row>
                    )}

                    <Table
                      dataSource={userCodStatement.items || []}
                      rowKey="payment_id"
                      pagination={false}
                      size="small"
                      columns={[
                        {
                          title: t('ui.users.order_number', 'Order'),
                          dataIndex: 'order_number',
                          key: 'order_number',
                        },
                        {
                          title: t('ui.users.status'),
                          dataIndex: 'status',
                          key: 'status',
                          render: (value) => <Tag>{value}</Tag>,
                        },
                        {
                          title: t('ui.users.total_amount', 'Amount'),
                          dataIndex: 'amount',
                          key: 'amount',
                          render: (value) => `${(value || 0).toLocaleString()} UZS`,
                        },
                        {
                          title: t('ui.users.amount_collected', 'Collected'),
                          dataIndex: 'amount_collected',
                          key: 'amount_collected',
                          render: (value) => `${(value || 0).toLocaleString()} UZS`,
                        },
                        {
                          title: t('ui.users.reserved_prepayment', 'Reserved'),
                          dataIndex: 'reserved_prepayment_amount',
                          key: 'reserved_prepayment_amount',
                          render: (value) => `${(value || 0).toLocaleString()} UZS`,
                        },
                        {
                          title: t('ui.users.net_outstanding', 'Net outstanding'),
                          dataIndex: 'net_outstanding_amount',
                          key: 'net_outstanding_amount',
                          render: (value, record) => {
                            const net = value ?? record.outstanding_amount ?? 0;
                            const formatted = `${net.toLocaleString()} UZS`;
                            const gross = record.outstanding_amount ?? 0;
                            if (gross > net) {
                              const reserved = record.reserved_prepayment_amount ?? 0;
                              return (
                                <Tooltip
                                  title={t(
                                    'ui.users.net_outstanding_tooltip',
                                    'Gross: {{gross}} UZS, reserved from prepayment: {{reserved}} UZS',
                                    {
                                      gross: gross.toLocaleString(),
                                      reserved: reserved.toLocaleString(),
                                    },
                                  )}
                                >
                                  <span style={{ fontWeight: 600 }}>{formatted}</span>
                                </Tooltip>
                              );
                            }
                            return <span style={{ fontWeight: 600 }}>{formatted}</span>;
                          },
                        },
                      ]}
                    />
                  </>
                ) : (
                  <div style={{ color: '#666' }}>
                    {t('ui.users.no_cod_statement', 'No COD debt found for this customer.')}
                  </div>
                )}
              </Card>
            )}

            {selectedUser.role === 'customer' && (
              <Card
                title={t('ui.users.prepayment_history', 'Prepayment history')}
                size="small"
                style={{ marginBottom: 12 }}
                extra={(
                  <Button
                    type="link"
                    size="small"
                    onClick={() => navigate(`/staff/prepayments?customer_id=${selectedUser.id}`)}
                  >
                    {t('ui.users.view_full_ledger', 'View full ledger')} →
                  </Button>
                )}
              >
                {userPrepaymentHistoryLoading ? (
                  <div style={{ textAlign: 'center', padding: '12px 0' }}>
                    {t('ui.common.loading', 'Loading...')}
                  </div>
                ) : userPrepaymentHistory && (userPrepaymentHistory.events || []).length > 0 ? (
                  <Table
                    dataSource={userPrepaymentHistory.events || []}
                    rowKey="id"
                    pagination={false}
                    size="small"
                    rowClassName={(row) => (row.voided_at ? 'prepayment-voided-row' : '')}
                    columns={[
                      {
                        title: t('ui.prepayments.occurred_at', 'When'),
                        dataIndex: 'occurred_at',
                        key: 'occurred_at',
                        render: (value, row) => (
                          <Space direction="vertical" size={0}>
                            <span>{value ? formatLocaleDateTime(value) : '—'}</span>
                            {row.voided_at && (
                              <Tag color="red" style={{ marginTop: 2 }}>
                                {t('ui.prepayments.voided', 'Voided')}
                              </Tag>
                            )}
                          </Space>
                        ),
                      },
                      {
                        title: t('ui.prepayments.source', 'Source'),
                        dataIndex: 'source',
                        key: 'source',
                        render: (value) => (
                          <Tag style={{ textTransform: 'capitalize' }}>
                            {String(value || '').replace(/_/g, ' ')}
                          </Tag>
                        ),
                      },
                      {
                        title: t('ui.prepayments.collected_amount', 'Collected'),
                        dataIndex: 'amount',
                        key: 'amount',
                        render: (value, row) => (
                          <span style={row.voided_at ? { textDecoration: 'line-through', color: '#999' } : undefined}>
                            {`${(value || 0).toLocaleString()} UZS`}
                          </span>
                        ),
                      },
                      {
                        title: t('ui.prepayments.unapplied_amount', 'Unapplied'),
                        dataIndex: 'unapplied_amount',
                        key: 'unapplied_amount',
                        render: (value, row) => (
                          <span
                            style={row.voided_at
                              ? { color: '#999' }
                              : (Number(value) > 0 ? { color: '#389e0d', fontWeight: 600 } : undefined)}
                          >
                            {`${(value || 0).toLocaleString()} UZS`}
                          </span>
                        ),
                      },
                      {
                        title: t('ui.prepayments.origin_order', 'Origin order'),
                        key: 'origin_order',
                        render: (_, row) => row.order_number || '—',
                      },
                    ]}
                  />
                ) : (
                  <div style={{ color: '#666' }}>
                    {t('ui.users.no_prepayment_history', 'No prepayment activity for this customer.')}
                  </div>
                )}
              </Card>
            )}

            {selectedUser.role === 'customer' && (
              <Card
                title={t('ui.users.cart', 'Cart')}
                size="small"
                style={{ marginBottom: 12 }}
              >
                {userCartLoading ? (
                  <div style={{ textAlign: 'center', padding: '12px 0' }}>
                    {t('ui.common.loading', 'Loading...')}
                  </div>
                ) : (userCart && userCart.cart_items && userCart.cart_items.length > 0) ? (
                  <>
                    <Table
                      dataSource={userCart.cart_items}
                      rowKey="id"
                      pagination={false}
                      size="small"
                      columns={[
                        {
                          title: t('ui.users.cart_product', 'Product'),
                          key: 'product',
                          render: (_value, record) => record.product?.name || `#${record.product_id}`,
                        },
                        {
                          title: t('ui.users.cart_quantity', 'Quantity'),
                          dataIndex: 'quantity',
                          key: 'quantity',
                        },
                        {
                          title: t('ui.users.cart_unit_price', 'Unit price'),
                          dataIndex: 'unit_price',
                          key: 'unit_price',
                          render: (value) => `${(value || 0).toLocaleString()} UZS`,
                        },
                        {
                          title: t('ui.users.cart_line_total', 'Line total'),
                          dataIndex: 'total_price',
                          key: 'total_price',
                          render: (value) => `${(value || 0).toLocaleString()} UZS`,
                        },
                      ]}
                    />
                    <Row gutter={[16, 12]} style={{ marginTop: 12 }}>
                      <Col xs={24} sm={8}>
                        <strong>{t('ui.users.cart_item_count', 'Items')}:</strong>{' '}
                        {userCart.item_count || 0}
                      </Col>
                      <Col xs={24} sm={8}>
                        <strong>{t('ui.users.cart_subtotal', 'Subtotal')}:</strong>{' '}
                        {(userCart.subtotal || 0).toLocaleString()} UZS
                      </Col>
                      <Col xs={24} sm={8}>
                        <strong>{t('ui.users.cart_total', 'Estimated total')}:</strong>{' '}
                        {(userCart.estimated_total || 0).toLocaleString()} UZS
                      </Col>
                    </Row>
                    {userCart.updated_at && (
                      <div style={{ color: '#666', fontSize: '12px', marginTop: 8 }}>
                        {t('ui.users.cart_updated_at', 'Last updated')}: {formatLocaleDateTime(userCart.updated_at)}
                      </div>
                    )}
                  </>
                ) : (
                  <div style={{ color: '#666' }}>
                    {t('ui.users.cart_empty', 'Cart is empty.')}
                  </div>
                )}
              </Card>
            )}

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
                        {selectedUser.last_bot_interaction ? formatLocaleDateTime(selectedUser.last_bot_interaction) : t('ui.users.never')}
                      </div>
                    </div>
                  </Col>
                </Row>
              </Card>
            )}

            <Card
              title={t('ui.users.notification_settings', 'Notification Settings')}
              size="small"
              style={{ marginBottom: 12 }}
            >
              {notificationSettingsLoading ? (
                <div style={{ textAlign: 'center', padding: '12px 0' }}>
                  {t('ui.common.loading', 'Loading...')}
                </div>
              ) : (
                <Space direction="vertical" size={10} style={{ width: '100%' }}>
                  <Row gutter={[12, 12]} align="middle" justify="space-between">
                    <Col xs={24} sm={16}>
                      <div style={{ fontWeight: 600 }}>
                        {t(
                          'ui.users.delivery_telegram_updates_setting',
                          'Telegram delivery updates (in transit, arrived)'
                        )}
                      </div>
                      <div style={{ fontSize: '12px', color: '#666', marginTop: 4 }}>
                        {t(
                          'ui.users.delivery_telegram_updates_setting_help',
                          'Controls Telegram notifications for delivery status changes.'
                        )}
                      </div>
                    </Col>
                    <Col xs={24} sm={8} style={{ textAlign: responsive.isMobileDevice ? 'left' : 'right' }}>
                      <Switch
                        checked={notificationSettings?.delivery_telegram_status_updates_enabled ?? true}
                        loading={updateUserNotificationSettingsMutation.isPending}
                        onChange={handleNotificationToggleRequest}
                      />
                    </Col>
                  </Row>

                  <Space wrap size={8}>
                    <Tag color={(notificationSettings?.delivery_telegram_status_updates_enabled ?? true) ? 'green' : 'red'}>
                      {(notificationSettings?.delivery_telegram_status_updates_enabled ?? true)
                        ? t('ui.users.notification_status_enabled', 'Enabled')
                        : t('ui.users.notification_status_disabled', 'Disabled')}
                    </Tag>
                    <Tag color={notificationSettings?.delivery_telegram_status_updates_source === 'explicit' ? 'blue' : 'default'}>
                      {notificationSettings?.delivery_telegram_status_updates_source === 'explicit'
                        ? t('ui.users.notification_source_explicit', 'Explicit')
                        : t('ui.users.notification_source_default', 'Default')}
                    </Tag>
                    <Tag color={notificationSettings?.telegram_connected ? 'processing' : 'default'}>
                      {notificationSettings?.telegram_connected
                        ? t('ui.users.telegram_connected', 'Telegram connected')
                        : t('ui.users.telegram_not_connected', 'Telegram not connected')}
                    </Tag>
                    <Tag color={notificationSettings?.bot_active ? 'processing' : 'default'}>
                      {notificationSettings?.bot_active
                        ? t('ui.users.bot_active', 'Bot active')
                        : t('ui.users.bot_inactive', 'Bot inactive')}
                    </Tag>
                  </Space>

                  {notificationSettingsError && (
                    <div style={{ color: '#ff4d4f', fontSize: '12px' }}>
                      {notificationSettingsError}
                    </div>
                  )}
                </Space>
              )}
            </Card>

            {/* Activity Info */}
            <Card title={t('ui.users.activity_information')} size="small" style={{ marginBottom: 12 }}>
              <Row gutter={[16, 12]}>
                <Col xs={24} sm={12}>
                  <div style={{ marginBottom: 8 }}>
                    <strong>{t('ui.users.created')}:</strong>
                    <div style={{ fontSize: '12px', marginTop: '2px' }}>
                      {formatLocaleDateTime(selectedUser.created_at)}
                    </div>
                  </div>
                  <div style={{ marginBottom: 8 }}>
                    <strong>{t('ui.users.updated')}:</strong>
                    <div style={{ fontSize: '12px', marginTop: '2px' }}>
                      {selectedUser.updated_at ? formatLocaleDateTime(selectedUser.updated_at) : t('ui.users.na')}
                    </div>
                  </div>
                  <div style={{ marginBottom: 8 }}>
                    <strong>{t('ui.users.date_of_birth', 'Date of Birth')}:</strong>
                    <div style={{ fontSize: '12px', marginTop: '2px' }}>
                      {selectedUser.date_of_birth ? selectedUser.date_of_birth.slice(0, 10) : t('ui.users.na')}
                    </div>
                  </div>
                </Col>
                <Col xs={24} sm={12}>
                  <div style={{ marginBottom: 8 }}>
                    <strong>{t('ui.users.last_login')}:</strong>
                    <div style={{ fontSize: '12px', marginTop: '2px' }}>
                      {selectedUser.last_login ? formatLocaleDateTime(selectedUser.last_login) : t('ui.users.never')}
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

      <Modal
        title={t('ui.users.notification_change_reason_title', 'Confirm Notification Setting Change')}
        open={isNotificationReasonModalVisible}
        onCancel={() => {
          setIsNotificationReasonModalVisible(false);
          setPendingNotificationToggle(null);
          setNotificationReason('');
        }}
        onOk={handleNotificationSettingsUpdateConfirm}
        confirmLoading={updateUserNotificationSettingsMutation.isPending}
        okText={t('ui.common.confirm', 'Confirm')}
        cancelText={t('ui.common.cancel', 'Cancel')}
      >
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <div style={{ fontSize: '13px' }}>
            {pendingNotificationToggle
              ? t(
                'ui.users.notification_change_reason_prompt_enable',
                'Please provide a reason for enabling Telegram delivery updates.'
              )
              : t(
                'ui.users.notification_change_reason_prompt_disable',
                'Please provide a reason for disabling Telegram delivery updates.'
              )}
          </div>
          <Input.TextArea
            rows={4}
            value={notificationReason}
            onChange={(event) => setNotificationReason(event.target.value)}
            placeholder={t('ui.users.notification_change_reason_placeholder', 'Enter reason')}
            maxLength={500}
          />
        </Space>
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
        confirmLoading={createAddressMutation.isPending || updateAddressMutation.isPending}
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
        title={editingUser ? t('ui.users.edit_user', 'Edit User') : t('ui.users.create_new_user', 'Create New User')}
        open={isCreateModalVisible}
        onCancel={() => {
          setIsCreateModalVisible(false);
          setEditingUser(null);
          createForm.resetFields();
        }}
        onOk={() => createForm.submit()}
        confirmLoading={createUserMutation.isPending || editUserMutation.isPending}
        okText={editingUser ? t('ui.common.save', 'Save') : t('ui.users.create', 'Create')}
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
          initialValues={{ user_type: 'individual' }}
          onFinish={handleCreateOrEditSubmit}
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
            name="date_of_birth"
            label={t('ui.users.date_of_birth', 'Date of Birth')}
          >
            <DatePicker
              style={{ width: '100%' }}
              format="YYYY-MM-DD"
              placeholder={t('ui.users.date_of_birth', 'Date of Birth')}
            />
          </Form.Item>

          <Row gutter={16}>
            <Col xs={24} sm={12}>
              <Form.Item
                name="user_type"
                label={t('ui.users.user_type', 'User Type')}
              >
                <Select
                  allowClear={!isEditingStaffUser}
                  disabled={isEditingStaffUser}
                  placeholder={t('ui.users.select_user_type', 'Select user type')}
                >
                  {(isEditingStaffUser
                    ? [{ value: 'staff', labelKey: 'ui.users.user_type_staff', fallback: 'Staff' }]
                    : USER_TYPE_OPTIONS
                  ).map((option) => (
                    <Option key={option.value} value={option.value}>
                      {t(option.labelKey, option.fallback)}
                    </Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
            <Col xs={24} sm={12}>
              <Form.Item
                name="company_name"
                label={t('ui.users.company_name', 'Company Name')}
                rules={[
                  {
                    validator: (_, value) => {
                      if (selectedUserType === 'entity' && !value?.trim()) {
                        return Promise.reject(new Error(t('ui.users.company_name_required', 'Company name is required for entity users')));
                      }
                      return Promise.resolve();
                    }
                  }
                ]}
              >
                <Input placeholder={t('ui.users.enter_company_name_optional', 'Enter company name (optional)')} />
              </Form.Item>
            </Col>
          </Row>

          {selectedUserType === 'entity' && (
            <Row gutter={16}>
              <Col xs={24} sm={12}>
                <Form.Item
                  name="entity_subtype"
                  label={t('ui.users.entity_subtype', 'Entity Subtype')}
                  rules={[
                    {
                      validator: (_, value) => {
                        if (!editingUser && !value) {
                          return Promise.reject(new Error(
                            t('ui.users.entity_subtype_required', 'Entity subtype is required for new entity users')
                          ));
                        }
                        return Promise.resolve();
                      }
                    }
                  ]}
                  extra={
                    selectedEntitySubtype === 'grocery_store'
                      ? t(
                          'ui.users.entity_subtype_grocery_hint',
                          'Grocery stores pay cash/card on or after delivery; debt is tracked in money. Business Account is unavailable.'
                        )
                      : selectedEntitySubtype === 'workplace'
                      ? t(
                          'ui.users.entity_subtype_workplace_hint',
                          'Workplaces prepay via Business Account; debt is tracked per product in bottle units.'
                        )
                      : null
                  }
                >
                  <Select
                    allowClear
                    placeholder={t('ui.users.select_entity_subtype', 'Select subtype')}
                  >
                    {ENTITY_SUBTYPE_OPTIONS.map((option) => (
                      <Option key={option.value} value={option.value}>
                        {t(option.labelKey, option.fallback)}
                      </Option>
                    ))}
                  </Select>
                </Form.Item>
              </Col>
              {editingUser && !selectedEntitySubtype && (
                <Col xs={24} sm={12}>
                  <div style={{
                    background: '#fff7e6',
                    border: '1px solid #ffd591',
                    borderRadius: 6,
                    padding: 12,
                    marginTop: 30
                  }}>
                    <strong>{t('ui.users.entity_subtype_unassigned', 'Subtype unassigned')}:</strong>{' '}
                    {t(
                      'ui.users.entity_subtype_unassigned_note',
                      'This customer cannot place orders until you assign a subtype.'
                    )}
                  </div>
                </Col>
              )}
            </Row>
          )}

          <Form.Item
            name="tax_id"
            label={t('ui.users.tax_id', 'Tax ID')}
            normalize={(value) => (typeof value === 'string' ? value.toUpperCase() : value)}
            rules={[
              {
                validator: (_, value) => {
                  if (!value) {
                    return Promise.resolve();
                  }
                  if (/^[A-Z0-9-]{5,20}$/.test(value)) {
                    return Promise.resolve();
                  }
                  return Promise.reject(new Error(t('ui.users.invalid_tax_id', 'Use 5-20 uppercase letters, digits, or dashes')));
                }
              }
            ]}
          >
            <Input placeholder={t('ui.users.enter_tax_id_optional', 'Enter tax ID (optional)')} />
          </Form.Item>

          {selectedUserType === 'entity' && (
            <div style={{
              background: '#e6f4ff',
              border: '1px solid #91caff',
              borderRadius: 6,
              padding: 12,
              marginBottom: 16
            }}>
              <strong>{t('ui.users.entity_client', 'Entity client')}:</strong>{' '}
              {t(
                'ui.users.entity_client_note',
                'Users created with user type "Entity" become selectable in the Corporate Contracts screen.'
              )}
            </div>
          )}

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

          {editingUser && (
            <>
              <Divider style={{ margin: '12px 0' }} />
              <Form.Item
                name="cod_debt_check_exempt"
                valuePropName="checked"
                label={t('ui.users.cod_debt_check_exempt', 'Exempt from COD debt limit')}
                tooltip={t(
                  'ui.users.cod_debt_check_exempt_tooltip',
                  'When enabled, this user can always order with cash on delivery regardless of outstanding COD debts. Use sparingly — bypasses financial safeguards. Every toggle is audited.'
                )}
                extra={t(
                  'ui.users.cod_debt_check_exempt_extra',
                  'Reserved for trusted customers (close partners, relatives).'
                )}
              >
                <Switch />
              </Form.Item>
            </>
          )}
        </Form>
      </Modal>
    </div>
  );
};

export default Users;
