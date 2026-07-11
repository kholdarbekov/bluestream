import { useEffect, useMemo, useRef, useState } from 'react';
import { DEFAULT_PAGE_SIZE, BULK_LOAD_PAGE_SIZE } from '../utils/constants';
import { fetchAllPages } from '../utils/pagination';
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
  Descriptions,
  Divider,
  Spin,
  Alert,
  Switch,
  InputNumber,
} from 'antd';
import {
  ShoppingCartOutlined,
  MoreOutlined,
  ExportOutlined,
  EyeOutlined,
  EditOutlined,
  DollarOutlined,
  PlusOutlined,
  UserOutlined,
  MinusCircleOutlined,
  ReloadOutlined,
  LinkOutlined,
  BarcodeOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { formatDate, formatDateTimeShort } from '../utils/dateUtils';
import { formatMoney } from '../utils/formatMoney';
import adminService from '../services/adminService';
import api from '../services/api';
import { useTranslation } from 'react-i18next';
import { extractApiErrorMessages } from '../utils/apiError';
import AsyncButton from '../components/common/AsyncButton';
import EmptyState from '../components/common/EmptyState';
import { usePermissions } from '../components/common/PermissionGuard';

const { Option } = Select;
const { RangePicker } = DatePicker;

const paymentStatusColor = (status) => {
  if (status === 'completed') return 'green';
  if (['pending', 'partially_paid'].includes(status)) return 'orange';
  if (status === 'not_required') return 'default';
  return 'red';
};

const fiscalizationStatusColor = (status) => {
  if (status === 'completed') return 'green';
  if (status === 'processing') return 'processing';
  if (status === 'not_required') return 'default';
  if (status === 'failed') return 'red';
  return 'orange';
};

// Fold free loyalty-reward lines into their matching purchased line as a "+N free"
// bonus; reward products with no purchased counterpart become standalone rows.
const buildRewardDisplayItems = (rawItems) => {
  const raw = rawItems || [];
  const freeByPid = {};
  raw.filter((i) => i.is_reward).forEach((i) => {
    freeByPid[i.product_id] = (freeByPid[i.product_id] || 0) + (i.quantity || 0);
  });
  const paidPids = new Set(raw.filter((i) => !i.is_reward).map((i) => i.product_id));
  const result = [];
  raw.filter((i) => !i.is_reward).forEach((i) => result.push({ ...i, bonusQty: freeByPid[i.product_id] || 0 }));
  const seen = new Set();
  raw.filter((i) => i.is_reward && !paidPids.has(i.product_id)).forEach((i) => {
    if (seen.has(i.product_id)) {
      const existing = result.find((d) => d.standalone && d.product_id === i.product_id);
      if (existing) existing.quantity += i.quantity || 0;
      return;
    }
    seen.add(i.product_id);
    result.push({ ...i, standalone: true });
  });
  return result;
};

const getOrderStatusColor = (status) => {
  switch (status) {
    case 'pending':
      return 'orange';
    case 'confirmed':
      return 'blue';
    case 'preparing':
      return 'cyan';
    case 'out_for_delivery':
      return 'purple';
    case 'delivered':
      return 'green';
    case 'cancelled':
      return 'red';
    case 'returned':
      return 'volcano';
    default:
      return 'default';
  }
};

const getMarkingActionColor = (action) => {
  switch (action) {
    case 'reserved':
      return 'blue';
    case 'used':
      return 'geekblue';
    case 'utilised':
      return 'green';
    case 'released':
      return 'orange';
    case 'created':
      return 'cyan';
    case 'imported':
      return 'purple';
    case 'restored':
      return 'gold';
    case 'archived':
    default:
      return 'default';
  }
};

const humanizeAuditAction = (value) =>
  value ? String(value).replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()) : '—';

const Orders = () => {
  const { t } = useTranslation('orders');
  const queryClient = useQueryClient();

  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [dateRange, setDateRange] = useState(null);
  const [fiscalizationFailedOnly, setFiscalizationFailedOnly] = useState(false);
  const [selectedOrder, setSelectedOrder] = useState(null);
  const [isDetailModalVisible, setIsDetailModalVisible] = useState(false);
  const [isStatusModalVisible, setIsStatusModalVisible] = useState(false);
  const [isCreateModalVisible, setIsCreateModalVisible] = useState(false);
  const [selectedUserId, setSelectedUserId] = useState(null);
  const [userAddresses, setUserAddresses] = useState([]);
  const [userPaymentMethods, setUserPaymentMethods] = useState([]);
  const [paymentRestrictions, setPaymentRestrictions] = useState(null);
  const [paymentMethodsLoading, setPaymentMethodsLoading] = useState(false);
  const [pagination, setPagination] = useState({ page: 1, per_page: DEFAULT_PAGE_SIZE });
  const [orderDetailsLoading, setOrderDetailsLoading] = useState(false);
  const [createOrderErrors, setCreateOrderErrors] = useState([]);
  const [isPersonalCardModalVisible, setIsPersonalCardModalVisible] = useState(false);
  // Order-edit modal (2-step flow): step 1 = form, step 2 = preview/confirm.
  const [isEditItemsModalVisible, setIsEditItemsModalVisible] = useState(false);
  const [editItemsStep, setEditItemsStep] = useState(1);
  const [editPreviewData, setEditPreviewData] = useState(null);
  const [editPreviewLoading, setEditPreviewLoading] = useState(false);
  // Snapshot the validated payload at step 1 → 2 transition. The Form
  // component unmounts when step 2 renders (preview view replaces it), so
  // we cannot rely on editItemsForm.getFieldsValue() in handleEditConfirm.
  const [pendingEditPayload, setPendingEditPayload] = useState(null);
  // Per-order edit history fetched into the detail modal.
  const [editHistoryEntries, setEditHistoryEntries] = useState([]);
  const [editHistoryLoading, setEditHistoryLoading] = useState(false);
  // Collected-cash edit modal (2-step flow): step 1 = form, step 2 = preview/confirm.
  const [isCashEditModalVisible, setIsCashEditModalVisible] = useState(false);
  const [cashEditStep, setCashEditStep] = useState(1);
  const [cashEditPreview, setCashEditPreview] = useState(null);
  // Snapshot the validated {new_amount, reason} at the step 1 → 2 transition.
  // The Form unmounts when step 2 renders, so cashEditForm.getFieldsValue() in
  // confirmCashEdit would return undefined fields (new_amount → NaN → JSON null).
  // Same reason the order-edit flow keeps pendingEditPayload above.
  const [pendingCashEdit, setPendingCashEdit] = useState(null);
  // Payment-method edit modal (2-step flow): step 1 = form, step 2 = preview/confirm.
  const [isPaymentMethodModalVisible, setIsPaymentMethodModalVisible] = useState(false);
  const [paymentMethodStep, setPaymentMethodStep] = useState(1);
  const [paymentMethodPreviewData, setPaymentMethodPreviewData] = useState(null);
  const [paymentMethodPreviewLoading, setPaymentMethodPreviewLoading] = useState(false);
  // Snapshot the validated {new_method, reason} at the step 1 → 2 transition,
  // same reason pendingEditPayload/pendingCashEdit exist: the Form unmounts
  // when step 2 renders.
  const [pendingPaymentMethodPayload, setPendingPaymentMethodPayload] = useState(null);

  const { isAdmin } = usePermissions();

  const [statusForm] = Form.useForm();
  const [createOrderForm] = Form.useForm();
  const [personalCardForm] = Form.useForm();
  const [editItemsForm] = Form.useForm();
  const [cashEditForm] = Form.useForm();
  const [paymentMethodForm] = Form.useForm();
  const watchedPaymentMethod = Form.useWatch('payment_method', createOrderForm);
  const watchedStatusValue = Form.useWatch('status', statusForm);

  const { data, isLoading } = useQuery({
    queryKey: ['orders', pagination, searchText, statusFilter, dateRange, fiscalizationFailedOnly],

    queryFn: () =>
      adminService.getOrders({
        page: pagination.page,
        per_page: pagination.per_page,
        search: searchText,
        status: statusFilter,
        start_date: dateRange?.[0]?.format('YYYY-MM-DD'),
        end_date: dateRange?.[1]?.format('YYYY-MM-DD'),
        ...(fiscalizationFailedOnly ? { fiscalization_failed: 'true' } : {}),
      }),

    placeholderData: keepPreviousData,
  });

  // Debounced server-side user search for the create-order picker.
  // Mirrors the pattern used in BottleTracking.js so admins can find any user,
  // not just the first page. See plan: docs reference per_page<=100 in PaginationMeta.
  const [userSearchTerm, setUserSearchTerm] = useState('');
  const userSearchDebounceRef = useRef();

  // The backend caps per_page at MAX_PAGE_SIZE (100), so a single request would
  // silently truncate the matches. Loop every page for the search term so the
  // picker shows ALL matching users, not just the first page.
  const { data: usersData, isFetching: isUsersFetching } = useQuery({
    queryKey: ['users-for-order', userSearchTerm],
    queryFn: () => fetchAllPages(
      (page) => adminService.getUsers({ search: userSearchTerm, page, per_page: BULK_LOAD_PAGE_SIZE }),
      (resp) => resp?.data?.items || [],
      BULK_LOAD_PAGE_SIZE,
    ),
    enabled: isCreateModalVisible && userSearchTerm.length >= 2,
    placeholderData: keepPreviousData,
  });

  const { data: selectedUserData } = useQuery({
    queryKey: ['user-for-order-selected', selectedUserId],
    queryFn: () => adminService.getUserDetails(selectedUserId),
    enabled: Boolean(selectedUserId),
  });

  const selectedUserRecord = selectedUserData?.data?.user || selectedUserData?.data || null;

  const userOptions = useMemo(() => {
    const items = usersData || [];
    const formatLabel = (u) => `${u.first_name || ''} ${u.last_name || ''}`.trim() + (u.phone ? ` - ${u.phone}` : '');
    const options = items.map((u) => ({ value: u.id, label: formatLabel(u) }));
    if (selectedUserRecord && !options.find((o) => o.value === selectedUserRecord.id)) {
      options.unshift({ value: selectedUserRecord.id, label: formatLabel(selectedUserRecord) });
    }
    return options;
  }, [usersData, selectedUserRecord]);

  const handleUserSearch = (value) => {
    if (userSearchDebounceRef.current) clearTimeout(userSearchDebounceRef.current);
    userSearchDebounceRef.current = setTimeout(() => setUserSearchTerm(value.trim()), 300);
  };

  // Clean up debounce timer on unmount.
  useEffect(() => () => {
    if (userSearchDebounceRef.current) clearTimeout(userSearchDebounceRef.current);
  }, []);

  const { data: productsData } = useQuery({
    queryKey: ['products-for-order', selectedUserId],

    queryFn: () =>
      adminService.getProducts({
        per_page: 100,
        is_active: true,
        ...(selectedUserId ? { pricing_user_id: selectedUserId } : {}),
      }),

    enabled: isCreateModalVisible || isEditItemsModalVisible,
  });

  const { data: statusesData } = useQuery({
    queryKey: ['order-statuses'],

    queryFn: async () => {
      const response = await api.get('/orders/statuses');
      return response.data;
    },

    staleTime: 1000 * 60 * 60 * 24,
  });
  const orderStatuses = statusesData?.data?.statuses || [];
  const statusTransitions = statusesData?.data?.transitions || {};
  const allowedNextStatuses = selectedOrder?.status
    ? new Set(statusTransitions[selectedOrder.status] || [])
    : null;

  const updateOrderMutation = useMutation({
    mutationFn: ({ orderId, status, notes, bottles_returned }) => adminService.updateOrderStatus(orderId, status, notes, { bottles_returned }),

    onSuccess: () => {
      message.success(t('ui.orders.status_updated_success', 'Order status updated successfully'));
      queryClient.invalidateQueries({
        queryKey: ['orders'],
      });
      setIsStatusModalVisible(false);
      statusForm.resetFields();
    },

    onError: (error) => {
      const errors = extractApiErrorMessages(error, t('ui.orders.status_update_failed', 'Failed to update order status'));
      message.error(errors[0]);
    },
  });

  const createOrderMutation = useMutation({
    mutationFn: (orderData) => adminService.createOrderForUser(orderData),

    onSuccess: (response) => {
      message.success(t('ui.orders.order_created_success', 'Order created successfully'));
      queryClient.invalidateQueries({
        queryKey: ['orders'],
      });
      setIsCreateModalVisible(false);
      createOrderForm.resetFields();
      setCreateOrderErrors([]);
      setSelectedUserId(null);
      setUserAddresses([]);
      setUserPaymentMethods([]);
      setPaymentRestrictions(null);
      if (userSearchDebounceRef.current) clearTimeout(userSearchDebounceRef.current);
      setUserSearchTerm('');

      const paymentUrl = response?.data?.payment_url;
      if (paymentUrl) {
        Modal.success({
          title: t('ui.orders.payment_link_ready', 'Payment link created'),
          content: (
            <a href={paymentUrl} target="_blank" rel="noreferrer">
              {paymentUrl}
            </a>
          ),
        });
      }
    },

    onError: (error) => {
      const errorMessages = extractApiErrorMessages(
        error,
        t('ui.orders.order_create_failed', 'Failed to create order'),
      );
      setCreateOrderErrors(errorMessages);
      message.error(errorMessages[0]);
    },
  });

  const recordPersonalCardPaymentMutation = useMutation({
    mutationFn: (payload) => adminService.recordStaffCashCollection(payload),

    onSuccess: async () => {
      message.success(t('ui.orders.personal_card_payment_recorded', 'Personal card payment recorded'));
      queryClient.invalidateQueries({
        queryKey: ['orders'],
      });
      setIsPersonalCardModalVisible(false);
      personalCardForm.resetFields();

      if (selectedOrder?.id) {
        try {
          const response = await adminService.getOrderDetails(selectedOrder.id);
          if (response.success && response.data?.order) {
            setSelectedOrder(response.data.order);
          }
        } catch (_error) {
          // Keep the current modal state when refresh fails.
        }
      }
    },

    onError: (error) => {
      const errorMessages = extractApiErrorMessages(
        error,
        t('ui.orders.personal_card_payment_failed', 'Failed to record personal card payment'),
      );
      message.error(errorMessages[0]);
    },
  });

  const submitOrderEditMutation = useMutation({
    mutationFn: ({ orderId, payload }) => adminService.submitOrderEdit(orderId, payload),
    onSuccess: async (response) => {
      const summary = response?.data?.cascade_summary || {};
      const warnings = response?.data?.warnings || [];
      message.success(t('ui.orders.edit_applied_success', 'Order updated successfully'));
      if (warnings.length) {
        Modal.warning({
          title: t('ui.orders.edit_warnings_title', 'Order updated with warnings'),
          content: (
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          ),
        });
      }
      queryClient.invalidateQueries({ queryKey: ['orders'] });
      // Refresh the detail modal payload + history.
      if (selectedOrder?.id) {
        try {
          const refreshed = await adminService.getOrderDetails(selectedOrder.id);
          if (refreshed.success && refreshed.data?.order) {
            setSelectedOrder(refreshed.data.order);
          }
          const history = await adminService.getOrderEditHistory(selectedOrder.id);
          if (history.success) {
            setEditHistoryEntries(history.data?.entries || []);
          }
        } catch (_err) {
          // best-effort refresh
        }
      }
      setIsEditItemsModalVisible(false);
      setEditItemsStep(1);
      setEditPreviewData(null);
      setPendingEditPayload(null);
      editItemsForm.resetFields();
      // Surface cascade impact so admin sees what happened at a glance.
      const cashAction = summary?.cash?.action;
      if (cashAction === 'prepayment_created') {
        message.info(
          t(
            'ui.orders.edit_prepayment_created',
            `Prepayment credit of ${formatMoney(summary.cash.amount)} UZS recorded for the customer.`,
          ),
        );
      } else if (cashAction === 'additional_cash_collection_required') {
        message.info(
          t(
            'ui.orders.edit_collect_extra_cash',
            `Collect ${formatMoney(summary.cash.amount)} UZS extra via Personal Card Payment.`,
          ),
        );
      }
    },
    onError: (error) => {
      const errors = extractApiErrorMessages(
        error,
        t('ui.orders.edit_failed', 'Failed to apply order edit'),
      );
      message.error(errors[0]);
    },
  });

  const openCashEdit = () => {
    cashEditForm.resetFields();
    cashEditForm.setFieldsValue({ new_amount: Number(selectedOrder.amount_collected || 0), reason: '' });
    setCashEditPreview(null);
    setPendingCashEdit(null);
    setCashEditStep(1);
    setIsCashEditModalVisible(true);
  };

  const handleCashEditPreview = async () => {
    let values;
    try {
      values = await cashEditForm.validateFields();
    } catch (e) {
      return; // invalid form — antd shows inline field errors, nothing else to do
    }
    // Snapshot the validated values NOW, while the Form (step 1) is still
    // mounted. confirmCashEdit reads this snapshot, not the unmounted form.
    const snapshot = { new_amount: Number(values.new_amount), reason: values.reason };
    setPendingCashEdit(snapshot);
    try {
      const resp = await adminService.previewCollectedCashEdit(selectedOrder.id, {
        new_amount: snapshot.new_amount,
      });
      setCashEditPreview(resp.data);
      setCashEditStep(2);
    } catch (err) {
      message.error(
        err?.response?.data?.message ||
          t('ui.orders.collected_cash_failed', 'Failed to preview collected cash')
      );
    }
  };

  const cashEditMutation = useMutation({
    mutationFn: ({ orderId, payload }) => adminService.editCollectedCash(orderId, payload),
    onSuccess: async (resp) => {
      message.success(t('ui.orders.collected_cash_updated', 'Collected cash updated'));
      const warnings = resp?.data?.warnings || [];
      if (warnings.length) {
        Modal.warning({ title: t('ui.orders.collected_cash_warnings', 'Please note'), content: warnings.join('\n') });
      }
      queryClient.invalidateQueries({ queryKey: ['orders'] });
      try {
        const refreshed = await adminService.getOrderDetails(selectedOrder.id);
        if (refreshed.success && refreshed.data?.order) {
          setSelectedOrder(refreshed.data.order);
        }
      } catch (_err) {
        // best-effort refresh
      }
      setIsCashEditModalVisible(false);
    },
    onError: (err) => {
      message.error(err?.response?.data?.message || t('ui.orders.collected_cash_failed', 'Failed to update collected cash'));
    },
  });

  const confirmCashEdit = () => {
    if (!pendingCashEdit) {
      message.error(t('ui.orders.edit_preview_missing', 'Preview the change before applying.'));
      setCashEditStep(1);
      return;
    }
    cashEditMutation.mutate({
      orderId: selectedOrder.id,
      payload: pendingCashEdit,
    });
  };

  const handleOpenPaymentMethodEdit = () => {
    if (!selectedOrder) return;
    paymentMethodForm.resetFields();
    paymentMethodForm.setFieldsValue({
      new_method: (selectedOrder.allowed_target_methods || [])[0],
      reason: '',
    });
    setPaymentMethodPreviewData(null);
    setPendingPaymentMethodPayload(null);
    setPaymentMethodStep(1);
    setIsPaymentMethodModalVisible(true);
  };

  const handleClosePaymentMethodEdit = () => {
    setIsPaymentMethodModalVisible(false);
    setPaymentMethodStep(1);
    setPaymentMethodPreviewData(null);
    setPendingPaymentMethodPayload(null);
    paymentMethodForm.resetFields();
  };

  const handlePaymentMethodPreview = async () => {
    if (!selectedOrder?.id) return;
    try {
      const values = await paymentMethodForm.validateFields();
      setPaymentMethodPreviewLoading(true);
      const payload = { new_method: values.new_method, reason: values.reason };
      const response = await adminService.previewOrderPaymentMethod(selectedOrder.id, {
        new_method: payload.new_method,
      });
      setPaymentMethodPreviewData(response?.data || null);
      // Cache the validated payload so handlePaymentMethodConfirm doesn't depend
      // on the Form component (which unmounts when step 2 renders).
      setPendingPaymentMethodPayload(payload);
      setPaymentMethodStep(2);
    } catch (error) {
      if (error?.errorFields) {
        // antd Form validation — let the form surface the issue.
        return;
      }
      const errors = extractApiErrorMessages(
        error,
        t('ui.orders.payment_method_preview_failed', 'Failed to preview payment-method change'),
      );
      message.error(errors[0]);
    } finally {
      setPaymentMethodPreviewLoading(false);
    }
  };

  const submitPaymentMethodMutation = useMutation({
    mutationFn: ({ orderId, payload }) => adminService.submitOrderPaymentMethod(orderId, payload),
    onSuccess: async (response) => {
      const warnings = response?.data?.warnings || [];
      const paymentLink = response?.data?.payment_link;
      message.success(t('ui.orders.payment_method_updated_success', 'Payment method updated successfully'));
      if (warnings.length) {
        Modal.warning({
          title: t('ui.orders.payment_method_warnings_title', 'Payment method updated with warnings'),
          content: (
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          ),
        });
      }
      if (paymentLink?.payment_url) {
        message.info(
          <span>
            {t('ui.orders.payment_method_link_ready', 'New payment link created: ')}
            <a href={paymentLink.payment_url} target="_blank" rel="noreferrer">
              {paymentLink.payment_url}
            </a>
          </span>,
        );
      }
      queryClient.invalidateQueries({ queryKey: ['orders'] });
      // Refresh the detail modal payload so the new method/eligibility show up.
      if (selectedOrder?.id) {
        try {
          const refreshed = await adminService.getOrderDetails(selectedOrder.id);
          if (refreshed.success && refreshed.data?.order) {
            setSelectedOrder(refreshed.data.order);
          }
        } catch (_err) {
          // best-effort refresh
        }
      }
      setIsPaymentMethodModalVisible(false);
      setPaymentMethodStep(1);
      setPaymentMethodPreviewData(null);
      setPendingPaymentMethodPayload(null);
      paymentMethodForm.resetFields();
    },
    onError: (error) => {
      const errors = extractApiErrorMessages(
        error,
        t('ui.orders.payment_method_update_failed', 'Failed to update payment method'),
      );
      message.error(errors[0]);
    },
  });

  const handlePaymentMethodConfirm = () => {
    if (!pendingPaymentMethodPayload) {
      message.error(t('ui.orders.edit_preview_missing', 'Preview the change before applying.'));
      setPaymentMethodStep(1);
      return;
    }
    submitPaymentMethodMutation.mutate({
      orderId: selectedOrder.id,
      payload: pendingPaymentMethodPayload,
    });
  };

  const retryFiscalizationMutation = useMutation({
    mutationFn: (paymentId) => adminService.retryPaymentFiscalization(paymentId),

    onSuccess: async () => {
      message.success(t('ui.orders.fiscalization_retry_success', 'Fiscalization retry queued successfully'));
      queryClient.invalidateQueries({
        queryKey: ['orders'],
      });
      if (selectedOrder?.id) {
        const response = await adminService.getOrderDetails(selectedOrder.id);
        if (response.success && response.data?.order) {
          setSelectedOrder(response.data.order);
        }
      }
    },

    onError: (error) => {
      const errors = extractApiErrorMessages(error, t('ui.orders.fiscalization_retry_failed', 'Failed to retry fiscalization'));
      message.error(errors[0]);
    },
  });

  const handleUserSelect = async (userId) => {
    setSelectedUserId(userId);
    createOrderForm.setFieldsValue({
      delivery_address_id: undefined,
      consume_marking_codes: false,
    });

    if (!userId) {
      setUserAddresses([]);
      setUserPaymentMethods([]);
      setPaymentRestrictions(null);
      return;
    }

    setPaymentMethodsLoading(true);
    try {
      const [addressResponse, paymentResponse] = await Promise.all([
        adminService.getUserAddresses(userId),
        adminService.getUserPaymentMethods(userId),
      ]);
      const addresses = addressResponse.data?.addresses || [];
      const paymentPayload = paymentResponse.data || {};
      const availableMethods = paymentPayload.available_methods || [];

      setUserAddresses(addresses);
      setUserPaymentMethods(availableMethods);
      setPaymentRestrictions(paymentPayload.payment_restrictions || null);
      createOrderForm.setFieldsValue({
        payment_method: (
          availableMethods.find((m) => m.is_default)
          || availableMethods.find((m) => m.method === 'business_account')
          || availableMethods[0]
        )?.method,
        consume_marking_codes: false,
      });
    } catch (error) {
      message.error(t('ui.orders.load_customer_context_failed', 'Failed to load customer payment context'));
      setUserAddresses([]);
      setUserPaymentMethods([]);
      setPaymentRestrictions(null);
    } finally {
      setPaymentMethodsLoading(false);
    }
  };

  const handleCreateOrderSubmit = (values) => {
    const allowedMethods = userPaymentMethods.map((method) => method.method);
    if (allowedMethods.length > 0 && !allowedMethods.includes(values.payment_method)) {
      message.error(t('ui.orders.payment_method_unavailable', 'Selected payment method is not available for this user'));
      return;
    }

    setCreateOrderErrors([]);
    createOrderMutation.mutate({
      user_id: values.user_id,
      delivery_address_id: values.delivery_address_id,
      payment_method: values.payment_method || 'cash',
      delivery_notes: values.delivery_notes || '',
      consume_marking_codes: values.payment_method === 'business_account' ? Boolean(values.consume_marking_codes) : false,
      items: values.items.map((item) => ({
        product_id: item.product_id,
        quantity: item.quantity,
      })),
    });
  };

  const handleViewOrder = async (order) => {
    setSelectedOrder(order);
    setIsDetailModalVisible(true);
    setOrderDetailsLoading(true);
    setEditHistoryEntries([]);
    setEditHistoryLoading(true);

    try {
      const response = await adminService.getOrderDetails(order.id);
      if (response.success && response.data?.order) {
        setSelectedOrder(response.data.order);
      }
    } catch (error) {
      // Keep lightweight table data if the detail request fails.
    } finally {
      setOrderDetailsLoading(false);
    }

    try {
      const historyResponse = await adminService.getOrderEditHistory(order.id);
      if (historyResponse.success) {
        setEditHistoryEntries(historyResponse.data?.entries || []);
      }
    } catch (_error) {
      // Order edit history is supplementary — fail soft.
    } finally {
      setEditHistoryLoading(false);
    }
  };

  const handleOpenEditItems = () => {
    if (!selectedOrder) return;
    const currentItems = (selectedOrder.items || []).map((item) => ({
      order_item_id: item.id,
      product_id: item.product_id,
      quantity: item.quantity,
    }));
    editItemsForm.resetFields();
    editItemsForm.setFieldsValue({ items: currentItems, reason: '' });
    setEditPreviewData(null);
    setEditItemsStep(1);
    setIsEditItemsModalVisible(true);
  };

  const handleCloseEditItems = () => {
    setIsEditItemsModalVisible(false);
    setEditItemsStep(1);
    setEditPreviewData(null);
    setPendingEditPayload(null);
    editItemsForm.resetFields();
  };

  const handleEditPreview = async () => {
    if (!selectedOrder?.id) return;
    try {
      const values = await editItemsForm.validateFields();
      setEditPreviewLoading(true);
      const payload = {
        items: (values.items || []).map((entry) => ({
          orderItemId: entry.order_item_id || null,
          productId: entry.product_id,
          quantity: Number(entry.quantity || 0),
        })),
        reason: values.reason,
      };
      const response = await adminService.previewOrderEdit(selectedOrder.id, payload);
      setEditPreviewData(response?.data || null);
      // Cache the validated payload so handleEditConfirm doesn't depend on
      // the Form component (which unmounts when step 2 renders).
      setPendingEditPayload(payload);
      setEditItemsStep(2);
    } catch (error) {
      if (error?.errorFields) {
        // antd Form validation — let the form surface the issue.
        return;
      }
      const errors = extractApiErrorMessages(
        error,
        t('ui.orders.edit_preview_failed', 'Failed to preview order edit'),
      );
      message.error(errors[0]);
    } finally {
      setEditPreviewLoading(false);
    }
  };

  const handleEditConfirm = () => {
    if (!pendingEditPayload) {
      message.error(t('ui.orders.edit_preview_missing', 'Preview the change before applying.'));
      setEditItemsStep(1);
      return;
    }
    submitOrderEditMutation.mutate({
      orderId: selectedOrder.id,
      payload: pendingEditPayload,
    });
  };

  const handleUpdateStatus = (order) => {
    setSelectedOrder(order);
    statusForm.setFieldsValue({
      status: order.status,
      notes: '',
    });
    setIsStatusModalVisible(true);
  };

  const handleCancelOrder = (order) => {
    Modal.confirm({
      title: t('ui.orders.cancel_order_title', 'Cancel order'),
      content: `${t('ui.orders.cancel_order_confirm', 'Cancel order')} ${order.order_number}?`,
      onOk: () => {
        updateOrderMutation.mutate({
          orderId: order.id,
          status: 'cancelled',
          notes: t('ui.orders.cancelled_by_admin', 'Cancelled by admin'),
        });
      },
    });
  };

  const orders = data?.data?.items || [];
  const totalRevenue = orders
    .filter((order) => !['cancelled', 'refunded'].includes(order.status))
    .reduce((sum, order) => sum + (order.total_amount || 0), 0);
  const pendingOrders = orders.filter((order) => order.status === 'pending').length;
  const clickOrders = orders.filter((order) => ['click', 'card'].includes(order.payment_provider || order.payment_method)).length;

  const getEffectiveProductPrice = (product) => {
    if (selectedUserId && product?.effective_unit_price !== undefined && product?.effective_unit_price !== null) {
      return product.effective_unit_price;
    }
    return product?.price;
  };

  const selectedOrderFiscalization = selectedOrder?.fiscalization || null;
  const selectedOrderMarkingSummary = useMemo(
    () => selectedOrder?.marking_code_summary || { events: {}, codes_by_order_item: {} },
    [selectedOrder?.marking_code_summary],
  );
  const selectedOrderPaymentTransactions = selectedOrder?.payment_transactions || [];
  const selectedOrderClickCallbacks = selectedOrder?.click_callback_history || [];
  const selectedOrderFiscalizationTrail = selectedOrder?.fiscalization_audit_trail || [];
  const selectedOrderMarkingActivity = selectedOrder?.marking_code_activity || [];

  // Derive merged display items for the detail modal: fold reward lines into their
  // matching purchased line as "+N free" bonus; standalone reward products get their
  // own row flagged as `standalone: true`.
  const detailDisplayItems = useMemo(
    () => buildRewardDisplayItems(selectedOrder?.items || selectedOrder?.items_summary || []),
    [selectedOrder?.items, selectedOrder?.items_summary],
  );

  const orderColumns = [
    {
      title: t('ui.orders.order_number', 'Order Number'),
      dataIndex: 'order_number',
      key: 'order_number',
      width: 140,
      render: (text) => <span style={{ fontFamily: 'monospace', fontWeight: 600 }}>{text}</span>,
    },
    {
      title: t('ui.orders.customer', 'Customer'),
      dataIndex: 'customer',
      key: 'customer',
      render: (_, record) => (
        <div>
          <div>{record.customer_name}</div>
          <small style={{ color: '#666' }}>{record.customer_email}</small>
        </div>
      ),
    },
    {
      title: t('ui.orders.items', 'Items'),
      dataIndex: 'items_summary',
      key: 'items_summary',
      width: 220,
      render: (items, record) => {
        if (!items || items.length === 0) {
          return <Tag color="blue">{record.items_count || 0} {t('ui.orders.items_count', 'items')}</Tag>;
        }
        // Merge reward lines into their paid counterparts for display
        const mergedItems = buildRewardDisplayItems(items);
        return (
          <div style={{ fontSize: 12 }}>
            {mergedItems.slice(0, 2).map((item) => (
              <div key={`${item.product_id || 'product'}-${item.product_name || 'item'}-${item.standalone ? 'r' : 'p'}`} style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {item.standalone
                  ? `🎁 ${item.product_name} ${t('ui.orders.free', 'Free')}`
                  : `${item.quantity}x ${item.product_name}${item.bonusQty > 0 ? ` (+${item.bonusQty} ${t('ui.orders.free', 'free')} 🎁)` : ''}`}
              </div>
            ))}
            {mergedItems.length > 2 ? <span style={{ color: '#999' }}>+{mergedItems.length - 2} {t('ui.orders.more_items', 'more')}</span> : null}
          </div>
        );
      },
    },
    {
      title: t('ui.orders.total_amount', 'Total Amount'),
      dataIndex: 'total_amount',
      key: 'total_amount',
      width: 130,
      render: (amount) => <span style={{ fontWeight: 600, color: '#52c41a' }}>{formatMoney(amount)} UZS</span>,
    },
    {
      title: t('ui.orders.status', 'Status'),
      dataIndex: 'status',
      key: 'status',
      width: 120,
      render: (status) => <Tag color={getOrderStatusColor(status)}>{t(`ui.orders.status_${status}`, status)}</Tag>,
    },
    {
      title: t('ui.orders.payment', 'Payment'),
      dataIndex: 'payment_status',
      key: 'payment_status',
      width: 120,
      render: (status) => <Tag color={paymentStatusColor(status)}>{t(`ui.orders.payment_${status}`, status || 'pending')}</Tag>,
    },
    {
      title: t('ui.orders.payment_provider', 'Provider'),
      dataIndex: 'payment_provider',
      key: 'payment_provider',
      width: 130,
      render: (value, record) => (value || record.payment_method || '—'),
    },
    {
      title: t('ui.orders.alerts', 'Alerts'),
      key: 'alerts',
      width: 200,
      render: (_, record) => {
        const tags = [];
        if (record.has_loyalty_reward) {
          tags.push(
            <Tag key="reward" color="gold" style={{ margin: '0 4px 2px 0' }}>
              🎁 {t('ui.orders.reward', 'Reward')}
            </Tag>,
          );
        }
        if (record.fiscalization_retries_exhausted) {
          tags.push(
            <Tag key="fisc" color="red" icon={<WarningOutlined />} style={{ margin: '0 4px 2px 0' }}>
              {t('ui.orders.fiscalization_retries_exhausted', 'Fiscalization Failed')}
            </Tag>,
          );
        }
        return tags.length > 0 ? <span>{tags}</span> : null;
      },
    },
    {
      title: t('ui.orders.order_date', 'Order Date'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 140,
      render: (date) => formatDate(date),
    },
    {
      title: t('ui.orders.actions', 'Actions'),
      key: 'actions',
      width: 100,
      render: (_, record) => (
        <Dropdown
          trigger={['click']}
          menu={{
            items: [
              {
                key: 'view',
                label: t('ui.orders.view_details', 'View Details'),
                icon: <EyeOutlined />,
                onClick: () => handleViewOrder(record),
              },
              {
                key: 'status',
                label: t('ui.orders.update_status', 'Update Status'),
                icon: <EditOutlined />,
                onClick: () => handleUpdateStatus(record),
              },
              { type: 'divider' },
              {
                key: 'cancel',
                label: t('ui.orders.cancel_order', 'Cancel Order'),
                danger: true,
                disabled: !(statusTransitions[record.status] || []).includes('cancelled'),
                onClick: () => handleCancelOrder(record),
              },
            ],
          }}
        >
          <Button type="text" icon={<MoreOutlined />} />
        </Dropdown>
      ),
    },
  ];

  const createOrderReset = () => {
    setIsCreateModalVisible(false);
    createOrderForm.resetFields();
    setCreateOrderErrors([]);
    setSelectedUserId(null);
    setUserAddresses([]);
    setUserPaymentMethods([]);
    setPaymentRestrictions(null);
    if (userSearchDebounceRef.current) clearTimeout(userSearchDebounceRef.current);
    setUserSearchTerm('');
  };

  const markingCodeRows = useMemo(() => {
    const entries = Object.entries(selectedOrderMarkingSummary.codes_by_order_item || {});
    return entries.map(([orderItemId, codes]) => ({
      orderItemId,
      codes,
    }));
  }, [selectedOrderMarkingSummary]);

  return (
    <div>
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic title={t('ui.orders.total_orders', 'Total Orders')} value={data?.meta?.total || 0} prefix={<ShoppingCartOutlined />} />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic title={t('ui.orders.total_revenue', 'Total Revenue')} value={totalRevenue} precision={2} prefix={<DollarOutlined />} suffix="UZS" />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic title={t('ui.orders.click_orders', 'Click/Card Orders')} value={clickOrders} prefix={<BarcodeOutlined />} />
          </Card>
        </Col>
      </Row>

      <Card style={{ marginBottom: 24 }}>
        <Statistic title={t('ui.orders.pending_orders', 'Pending Orders')} value={pendingOrders} valueStyle={{ color: '#faad14' }} />
      </Card>

      <Card>
        <div className="table-actions">
          <Space wrap>
            <Input.Search
              placeholder={t('ui.orders.search_placeholder', 'Search orders')}
              allowClear
              onSearch={(value) => {
                setSearchText(value);
                setPagination((current) => ({ ...current, page: 1 }));
              }}
              style={{ width: 250 }}
            />
            <Select
              placeholder={t('ui.orders.filter_by_status', 'Filter by status')}
              allowClear
              onChange={(value) => {
                setStatusFilter(value || '');
                setPagination((current) => ({ ...current, page: 1 }));
              }}
              style={{ width: 180 }}
            >
              {orderStatuses.map((status) => (
                <Option key={status.value} value={status.value}>
                  {t(`ui.orders.status_${status.value}`, status.label)}
                </Option>
              ))}
            </Select>
            <RangePicker
              onChange={(dates) => {
                setDateRange(dates);
                setPagination((current) => ({ ...current, page: 1 }));
              }}
              format="YYYY-MM-DD"
              placeholder={[t('ui.orders.start_date', 'Start date'), t('ui.orders.end_date', 'End date')]}
            />
            <Space size={6}>
              <WarningOutlined style={{ color: fiscalizationFailedOnly ? '#cf1322' : '#bfbfbf' }} />
              <span>{t('ui.orders.fiscalization_failed_only', 'Fiscalization failed only')}</span>
              <Switch
                checked={fiscalizationFailedOnly}
                onChange={(checked) => {
                  setFiscalizationFailedOnly(checked);
                  setPagination((current) => ({ ...current, page: 1 }));
                }}
              />
            </Space>
          </Space>

          <Space>
            <Button icon={<ExportOutlined />} disabled>
              {t('ui.orders.export_orders', 'Export Orders')}
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setIsCreateModalVisible(true)}>
              {t('ui.orders.create_order', 'Create Order')}
            </Button>
          </Space>
        </div>

        <Table
          columns={orderColumns}
          dataSource={orders}
          loading={isLoading}
          rowKey="id"
          locale={{
            emptyText: <EmptyState description={t('ui.orders.no_orders', 'No orders found')} />,
          }}
          pagination={{
            current: pagination.page,
            pageSize: pagination.per_page,
            total: data?.meta?.total || 0,
            showSizeChanger: true,
            showQuickJumper: true,
            showTotal: (total, range) => `${range[0]}-${range[1]} of ${total} ${t('ui.orders.pagination_text', 'orders')}`,
          }}
          onChange={(paginationInfo) => {
            setPagination({
              page: paginationInfo.current,
              per_page: paginationInfo.pageSize,
            });
          }}
          className="admin-table"
          scroll={{ x: 1200 }}
        />
      </Card>

      <Modal
        title={`${t('ui.orders.order_details', 'Order Details')} - ${selectedOrder?.order_number || ''}`}
        open={isDetailModalVisible}
        onCancel={() => setIsDetailModalVisible(false)}
        footer={null}
        width={980}
      >
        {selectedOrder ? (
          <div>
            <Descriptions column={2} bordered>
              <Descriptions.Item label={t('ui.orders.order_number', 'Order Number')}>
                {selectedOrder.order_number}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.status', 'Status')}>
                <Tag color={getOrderStatusColor(selectedOrder.status)}>
                  {t(`ui.orders.status_${selectedOrder.status}`, selectedOrder.status)}
                </Tag>
                {selectedOrder.has_loyalty_reward ? (
                  <Tag color="gold" style={{ marginLeft: 4 }}>
                    🎁 {t('ui.orders.reward', 'Reward')}
                  </Tag>
                ) : null}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.customer', 'Customer')}>
                {selectedOrder.customer_name}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.email', 'Email')}>
                {selectedOrder.customer_email || '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.phone', 'Phone')}>
                {selectedOrder.customer_phone || '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.total_amount', 'Total Amount')}>
                <span style={{ fontWeight: 600, color: '#52c41a' }}>{formatMoney(selectedOrder.total_amount)} UZS</span>
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.payment_status', 'Payment Status')}>
                <Tag color={paymentStatusColor(selectedOrder.payment_status)}>
                  {t(`ui.orders.payment_${selectedOrder.payment_status}`, selectedOrder.payment_status || 'pending')}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.payment_method', 'Payment Method')}>
                {selectedOrder.payment_method || '—'}
                {isAdmin() && selectedOrder.is_payment_method_editable ? (
                  <Button
                    type="link"
                    size="small"
                    icon={<EditOutlined />}
                    onClick={handleOpenPaymentMethodEdit}
                    style={{ marginLeft: 8, padding: 0 }}
                  >
                    {t('ui.orders.edit_payment_method', 'Change')}
                  </Button>
                ) : null}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.origin', 'Origin')}>
                {selectedOrder.is_subscription_order ? (
                  <Tag color="blue">
                    {t('ui.orders.from_subscription', 'Subscription')} #{selectedOrder.subscription_id}
                  </Tag>
                ) : (
                  t('ui.orders.one_off', 'One-off')
                )}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.payment_provider', 'Payment Provider')}>
                {selectedOrder.payment_provider || '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.provider_transaction_id', 'Provider Transaction ID')}>
                {selectedOrder.provider_transaction_id || '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.order_date', 'Order Date')}>
                {formatDateTimeShort(selectedOrder.created_at)}
              </Descriptions.Item>
            </Descriptions>

            <Divider>{t('ui.orders.payment_summary', 'Payment Summary')}</Divider>
            <Descriptions column={3} bordered size="small">
              <Descriptions.Item label={t('ui.orders.total_amount', 'Total Amount')}>
                {formatMoney(selectedOrder.total_amount)} UZS
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.amount_collected', 'Collected')}>
                {formatMoney(selectedOrder.amount_collected)} UZS
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.outstanding_amount', 'Outstanding')}>
                {formatMoney(selectedOrder.outstanding_amount)} UZS
              </Descriptions.Item>
            </Descriptions>
            {isAdmin() && selectedOrder.is_collected_cash_editable ? (
              <Button
                icon={<EditOutlined />}
                onClick={openCashEdit}
                style={{ marginTop: 8 }}
              >
                {t('ui.orders.edit_collected_cash', 'Edit collected cash')}
                {selectedOrder.collected_cash_edit_window_remaining_hours != null
                  ? ` (${selectedOrder.collected_cash_edit_window_remaining_hours.toFixed(1)}h left)`
                  : ''}
              </Button>
            ) : null}

            <Divider>{t('ui.orders.fiscalization', 'Fiscalization')}</Divider>
            <Descriptions column={2} bordered size="small">
              <Descriptions.Item label={t('ui.orders.fiscalization_status', 'Fiscalization Status')}>
                <Tag color={fiscalizationStatusColor(selectedOrder.fiscalization_status)}>
                  {t(`ui.orders.fiscalization_${selectedOrder.fiscalization_status}`, selectedOrder.fiscalization_status || 'pending')}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.consume_marking_codes', 'Consume Marking Codes')}>
                {selectedOrder.consume_marking_codes ? t('ui.common.yes', 'Yes') : t('ui.common.no', 'No')}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.payment_link', 'Payment Link')}>
                {selectedOrder.payment_link ? (
                  <a href={selectedOrder.payment_link} target="_blank" rel="noreferrer">
                    {t('ui.orders.open_payment_link', 'Open payment link')}
                  </a>
                ) : '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.receipt_link', 'Receipt Link')}>
                {selectedOrderFiscalization?.provider_receipt_url ? (
                  <a href={selectedOrderFiscalization.provider_receipt_url} target="_blank" rel="noreferrer">
                    {t('ui.orders.open_receipt', 'Open receipt')}
                  </a>
                ) : '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.receipt_id', 'Receipt ID')}>
                {selectedOrderFiscalization?.provider_receipt_id || '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.fiscalization_failure_reason', 'Failure Reason')}>
                {selectedOrderFiscalization?.failure_reason || '—'}
              </Descriptions.Item>
            </Descriptions>

            {selectedOrder?.payment_provider && ['click', 'card'].includes(selectedOrder.payment_provider) && selectedOrder.fiscalization_status !== 'completed' && selectedOrder.fiscalization_status !== 'not_required' ? (
              <div style={{ marginTop: 16 }}>
                <AsyncButton
                  icon={<ReloadOutlined />}
                  disabled={!selectedOrder.payment_id}
                  loading={retryFiscalizationMutation.isPending}
                  onClick={() => retryFiscalizationMutation.mutateAsync(selectedOrder.payment_id)}
                >
                  {t('ui.orders.retry_fiscalization', 'Retry Fiscalization')}
                </AsyncButton>
              </div>
            ) : null}

            <Divider>{t('ui.orders.fiscalization_audit_trail', 'Fiscalization Audit Trail')}</Divider>
            {selectedOrderFiscalizationTrail.length ? (
              <Table
                dataSource={selectedOrderFiscalizationTrail}
                rowKey={(record) => `${record.action || 'event'}-${record.occurred_at || 'unknown'}-${record.actor_user_id ?? 'na'}`}
                pagination={false}
                size="small"
                columns={[
                  {
                    title: t('ui.orders.audit_action', 'Action'),
                    dataIndex: 'action',
                    key: 'action',
                    render: (value) => humanizeAuditAction(value),
                  },
                  {
                    title: t('ui.orders.audit_status', 'Status'),
                    dataIndex: 'success',
                    key: 'success',
                    render: (value) => (
                      <Tag color={value ? 'green' : 'red'}>
                        {value ? t('ui.common.success', 'Success') : t('ui.common.failed', 'Failed')}
                      </Tag>
                    ),
                  },
                  {
                    title: t('ui.orders.receipt_id', 'Receipt ID'),
                    key: 'provider_receipt_id',
                    render: (_, record) => record?.additional_data?.provider_receipt_id || '—',
                  },
                  {
                    title: t('ui.orders.error', 'Error'),
                    dataIndex: 'error_message',
                    key: 'error_message',
                    render: (value) => value || '—',
                  },
                  {
                    title: t('ui.orders.time', 'Time'),
                    dataIndex: 'occurred_at',
                    key: 'occurred_at',
                    render: (value) => formatDateTimeShort(value),
                  },
                ]}
              />
            ) : (
              <Alert type="info" showIcon message={t('ui.orders.no_fiscalization_audit_trail', 'No fiscalization audit trail recorded yet')} />
            )}

            <Divider>{t('ui.orders.payment_transactions', 'Payment Transactions')}</Divider>
            {selectedOrderPaymentTransactions.length ? (
              <Table
                dataSource={selectedOrderPaymentTransactions}
                rowKey="id"
                pagination={false}
                size="small"
                columns={[
                  {
                    title: t('ui.orders.transaction_type', 'Type'),
                    dataIndex: 'transaction_type',
                    key: 'transaction_type',
                  },
                  {
                    title: t('ui.orders.transaction_status', 'Status'),
                    dataIndex: 'status',
                    key: 'status',
                    render: (value, record) => (
                      <Tag color={record?.success ? 'green' : 'red'}>
                        {value || '—'}
                      </Tag>
                    ),
                  },
                  {
                    title: t('ui.orders.provider_transaction_id', 'Provider Transaction ID'),
                    dataIndex: 'provider_transaction_id',
                    key: 'provider_transaction_id',
                    render: (value) => value || '—',
                  },
                  {
                    title: t('ui.orders.notes', 'Notes'),
                    key: 'notes',
                    render: (_, record) => record.failure_reason || record.notes || '—',
                  },
                  {
                    title: t('ui.orders.time', 'Time'),
                    dataIndex: 'created_at',
                    key: 'created_at',
                    render: (value) => formatDateTimeShort(value),
                  },
                ]}
              />
            ) : (
              <Alert type="info" showIcon message={t('ui.orders.no_payment_transactions', 'No payment transactions recorded yet')} />
            )}

            <Divider>{t('ui.orders.click_callback_history', 'Click Callback History')}</Divider>
            {selectedOrderClickCallbacks.length ? (
              <Table
                dataSource={selectedOrderClickCallbacks}
                rowKey={(record) => `${record.stage || 'callback'}-${record.received_at || 'unknown'}-${record?.response?.error ?? 'na'}`}
                pagination={false}
                size="small"
                columns={[
                  {
                    title: t('ui.orders.callback_stage', 'Stage'),
                    dataIndex: 'stage',
                    key: 'stage',
                  },
                  {
                    title: t('ui.orders.callback_result', 'Result'),
                    key: 'result',
                    render: (_, record) => {
                      const responseError = record?.response?.error;
                      if (responseError === 0) {
                        return <Tag color="green">{t('ui.common.success', 'Success')}</Tag>;
                      }
                      if (responseError !== undefined && responseError !== null) {
                        return <Tag color="red">{`${responseError}`}</Tag>;
                      }
                      return '—';
                    },
                  },
                  {
                    title: t('ui.orders.callback_note', 'Note'),
                    key: 'note',
                    render: (_, record) => record?.response?.error_note || record?.request?.error_note || '—',
                  },
                  {
                    title: t('ui.orders.time', 'Time'),
                    dataIndex: 'received_at',
                    key: 'received_at',
                    render: (value) => formatDateTimeShort(value),
                  },
                ]}
              />
            ) : (
              <Alert type="info" showIcon message={t('ui.orders.no_click_callbacks', 'No Click callback history recorded yet')} />
            )}

            <Divider>{t('ui.orders.marking_code_summary', 'Marking-Code Summary')}</Divider>
            <Row gutter={[16, 16]}>
              {Object.entries(selectedOrderMarkingSummary.events || {}).length ? (
                Object.entries(selectedOrderMarkingSummary.events || {}).map(([event, count]) => (
                  <Col xs={12} md={6} key={event}>
                    <Card>
                      <Statistic title={t(`ui.orders.marking_code_event_${event}`, event)} value={count} />
                    </Card>
                  </Col>
                ))
              ) : (
                <Col span={24}>
                  <Alert type="info" showIcon message={t('ui.orders.no_marking_code_activity', 'No marking-code activity recorded for this order')} />
                </Col>
              )}
            </Row>

            {markingCodeRows.length ? (
              <Table
                style={{ marginTop: 16 }}
                dataSource={markingCodeRows}
                rowKey="orderItemId"
                pagination={false}
                size="small"
                columns={[
                  {
                    title: t('ui.orders.order_item', 'Order Item'),
                    dataIndex: 'orderItemId',
                    key: 'orderItemId',
                    render: (value) => `#${value}`,
                  },
                  {
                    title: t('ui.orders.marking_codes', 'Marking Codes'),
                    dataIndex: 'codes',
                    key: 'codes',
                    render: (codes) => (
                      <Space wrap>
                        <Tag color="blue">
                          {t('ui.orders.marking_codes_count', '{{count}} codes').replace('{{count}}', (codes || []).length)}
                        </Tag>
                        {(codes || []).map((code) => (
                          <Tag key={code} style={{ fontFamily: 'monospace' }}>{code}</Tag>
                        ))}
                      </Space>
                    ),
                  },
                ]}
              />
            ) : null}

            <Divider>{t('ui.orders.marking_code_activity', 'Marking-Code Activity')}</Divider>
            {selectedOrderMarkingActivity.length ? (
              <Table
                dataSource={selectedOrderMarkingActivity}
                rowKey="id"
                pagination={false}
                size="small"
                columns={[
                  {
                    title: t('ui.orders.action', 'Action'),
                    dataIndex: 'action',
                    key: 'action',
                    render: (value) => value ? (
                      <Tag color={getMarkingActionColor(value)}>
                        {t(`ui.orders.marking_code_event_${value}`, value)}
                      </Tag>
                    ) : '—',
                  },
                  {
                    title: t('ui.orders.marking_code', 'Marking Code'),
                    dataIndex: 'code',
                    key: 'code',
                    render: (value) => value ? <Tag style={{ fontFamily: 'monospace' }}>{value}</Tag> : '—',
                  },
                  {
                    title: t('ui.orders.order_item', 'Order Item'),
                    dataIndex: 'order_item_id',
                    key: 'order_item_id',
                    render: (value) => `#${value}`,
                  },
                  {
                    title: t('ui.orders.notes', 'Notes'),
                    key: 'notes',
                    render: (_, record) => record.notes || record?.event_metadata?.reason || '—',
                  },
                  {
                    title: t('ui.orders.time', 'Time'),
                    dataIndex: 'occurred_at',
                    key: 'occurred_at',
                    render: (value) => formatDateTimeShort(value),
                  },
                ]}
              />
            ) : (
              <Alert type="info" showIcon message={t('ui.orders.no_marking_code_activity', 'No marking-code activity recorded for this order')} />
            )}

            <Divider>{t('ui.orders.order_items', 'Order Items')}</Divider>
            <Spin spinning={orderDetailsLoading}>
              <Table
                dataSource={detailDisplayItems}
                rowKey={(record) => record.id ?? `${record.product_id}-${record.standalone ? 'r' : 'p'}`}
                pagination={false}
                size="small"
                columns={[
                  {
                    title: t('ui.orders.product_name', 'Product'),
                    dataIndex: 'product_name',
                    key: 'product_name',
                    render: (name, record) => (
                      <span>
                        {name}
                        {(record.bonusQty > 0 || record.standalone) ? (
                          <Tag color="gold" style={{ marginLeft: 6 }}>
                            🎁 {t('ui.orders.reward', 'Reward')}
                          </Tag>
                        ) : null}
                      </span>
                    ),
                  },
                  {
                    title: t('ui.orders.quantity', 'Qty'),
                    dataIndex: 'quantity',
                    key: 'quantity',
                    width: 120,
                    align: 'center',
                    render: (qty, record) => (
                      <span>
                        {qty}
                        {record.bonusQty > 0 ? ` (+${record.bonusQty} ${t('ui.orders.free', 'free')})` : ''}
                      </span>
                    ),
                  },
                  {
                    title: t('ui.orders.unit_price', 'Unit Price'),
                    dataIndex: 'unit_price',
                    key: 'unit_price',
                    width: 140,
                    align: 'right',
                    render: (price, record) =>
                      record.standalone
                        ? <span style={{ color: '#52c41a', fontWeight: 600 }}>{t('ui.orders.free', 'Free')}</span>
                        : `${formatMoney(price)} UZS`,
                  },
                  {
                    title: t('ui.orders.total_price', 'Total'),
                    dataIndex: 'total_price',
                    key: 'total_price',
                    width: 140,
                    align: 'right',
                    render: (price, record) =>
                      record.standalone
                        ? <span style={{ fontWeight: 600, color: '#52c41a' }}>{t('ui.orders.free', 'Free')}</span>
                        : <span style={{ fontWeight: 600 }}>{formatMoney(price)} UZS</span>,
                  },
                ]}
                footer={() => (
                  <div style={{ textAlign: 'right' }}>
                    <strong>{t('ui.orders.order_total', 'Order Total')}: </strong>
                    <span style={{ fontSize: 16, color: '#52c41a', fontWeight: 600 }}>
                      {formatMoney(selectedOrder.total_amount)} UZS
                    </span>
                  </div>
                )}
              />
            </Spin>

            <Divider>{t('ui.orders.order_changes', 'Order Changes')}</Divider>
            {editHistoryLoading ? (
              <Spin />
            ) : editHistoryEntries.length ? (
              <Table
                dataSource={editHistoryEntries}
                rowKey="id"
                pagination={false}
                size="small"
                expandable={{
                  expandedRowRender: (record) => (
                    <div style={{ padding: 8 }}>
                      <Descriptions size="small" column={2} bordered>
                        <Descriptions.Item label={t('ui.orders.totals_before', 'Totals before')}>
                          {formatMoney(record.diff?.totals_before?.total_amount)} UZS
                        </Descriptions.Item>
                        <Descriptions.Item label={t('ui.orders.totals_after', 'Totals after')}>
                          {formatMoney(record.diff?.totals_after?.total_amount)} UZS
                        </Descriptions.Item>
                      </Descriptions>
                      <div style={{ marginTop: 8 }}>
                        <strong>{t('ui.orders.items_before', 'Items before')}:</strong>
                        <ul style={{ margin: '4px 0 8px 16px' }}>
                          {(record.diff?.items_before || []).map((it, idx) => (
                            <li key={`b-${idx}`}>
                              {it.product_name || it.product?.name || `#${it.product_id}`} × {it.quantity}
                            </li>
                          ))}
                        </ul>
                        <strong>{t('ui.orders.items_after', 'Items after')}:</strong>
                        <ul style={{ margin: '4px 0 8px 16px' }}>
                          {(record.diff?.items_after || []).map((it, idx) => (
                            <li key={`a-${idx}`}>
                              {it.product_name || it.product?.name || `#${it.product_id}`} × {it.quantity}
                            </li>
                          ))}
                        </ul>
                        {(record.diff?.warnings || []).length ? (
                          <Alert
                            type="warning"
                            showIcon
                            message={t('ui.orders.edit_warnings', 'Warnings')}
                            description={
                              <ul style={{ margin: 0, paddingLeft: 18 }}>
                                {(record.diff.warnings || []).map((warning) => (
                                  <li key={warning}>{warning}</li>
                                ))}
                              </ul>
                            }
                          />
                        ) : null}
                      </div>
                    </div>
                  ),
                }}
                columns={[
                  {
                    title: t('ui.orders.edited_at', 'When'),
                    dataIndex: 'edited_at',
                    key: 'edited_at',
                    render: (value) => formatDateTimeShort(value),
                  },
                  {
                    title: t('ui.orders.edited_by', 'By'),
                    key: 'edited_by',
                    render: (_, record) => record.edited_by_user?.name || record.edited_by_user_id,
                  },
                  {
                    title: t('ui.orders.edit_reason', 'Reason'),
                    dataIndex: 'reason',
                    key: 'reason',
                  },
                  {
                    title: t('ui.orders.edit_post_delivery', 'Post-delivery'),
                    dataIndex: 'is_post_delivery',
                    key: 'is_post_delivery',
                    render: (value) => (
                      <Tag color={value ? 'orange' : 'default'}>
                        {value ? t('ui.common.yes', 'Yes') : t('ui.common.no', 'No')}
                      </Tag>
                    ),
                  },
                ]}
              />
            ) : (
              <Alert type="info" showIcon message={t('ui.orders.no_edit_history', 'No admin edits recorded for this order')} />
            )}

            {selectedOrder.payment_timeline?.timeline?.length ? (
              <>
                <Divider>{t('ui.orders.payment_timeline', 'Payment Timeline')}</Divider>
                <Table
                  dataSource={selectedOrder.payment_timeline.timeline}
                  rowKey={(record) => `${record.type}-${record.timestamp || record.notes || 'row'}`}
                  pagination={false}
                  size="small"
                  columns={[
                    {
                      title: t('ui.orders.timeline_type', 'Type'),
                      dataIndex: 'type',
                      key: 'type',
                    },
                    {
                      title: t('ui.orders.timeline_timestamp', 'Timestamp'),
                      dataIndex: 'timestamp',
                      key: 'timestamp',
                    },
                    {
                      title: t('ui.orders.timeline_amount', 'Amount'),
                      key: 'amount',
                      render: (_, record) => `${formatMoney(record.allocated_amount ?? record.amount)} UZS`,
                    },
                    {
                      title: t('ui.orders.timeline_notes', 'Notes'),
                      dataIndex: 'notes',
                      key: 'notes',
                      render: (value) => value || '—',
                    },
                  ]}
                />
              </>
            ) : null}

            <div style={{ marginTop: 16, textAlign: 'right' }}>
              <Space>
                {selectedOrder.payment_link ? (
                  <Button icon={<LinkOutlined />} href={selectedOrder.payment_link} target="_blank">
                    {t('ui.orders.open_payment_link', 'Open Payment Link')}
                  </Button>
                ) : null}
                {(selectedOrder.payment_method === 'cash' ||
                  (['click', 'payme', 'card'].includes(selectedOrder.payment_method) &&
                   ['pending', 'cancelled', 'failed'].includes(selectedOrder.payment_status))) ? (
                  <Button
                    icon={<DollarOutlined />}
                    disabled={['cancelled', 'returned'].includes(selectedOrder.status)}
                    onClick={() => {
                      personalCardForm.setFieldsValue({
                        amount: selectedOrder.outstanding_amount || 0,
                        notes: '',
                      });
                      setIsPersonalCardModalVisible(true);
                    }}
                  >
                    {t('ui.orders.record_personal_card_payment', 'Record Personal Card Payment')}
                  </Button>
                ) : null}
                {selectedOrder.is_editable ? (
                  <Button
                    icon={<EditOutlined />}
                    onClick={handleOpenEditItems}
                  >
                    {t('ui.orders.edit_items', 'Edit Items')}
                    {selectedOrder.edit_window_remaining_hours != null
                      ? ` (${selectedOrder.edit_window_remaining_hours.toFixed(1)}h left)`
                      : ''}
                  </Button>
                ) : null}
                <Button
                  type="primary"
                  onClick={() => {
                    setIsDetailModalVisible(false);
                    handleUpdateStatus(selectedOrder);
                  }}
                >
                  {t('ui.orders.update_status', 'Update Status')}
                </Button>
                <Button onClick={() => setIsDetailModalVisible(false)}>{t('ui.orders.close', 'Close')}</Button>
              </Space>
            </div>
          </div>
        ) : null}
      </Modal>

      <Modal
        title={`${t('ui.orders.update_order_status', 'Update Order Status')} - ${selectedOrder?.order_number || ''}`}
        open={isStatusModalVisible}
        onCancel={() => setIsStatusModalVisible(false)}
        footer={null}
      >
        <Form form={statusForm} layout="vertical" onFinish={(values) => {
          updateOrderMutation.mutate({
            orderId: selectedOrder.id,
            status: values.status,
            notes: values.notes,
            ...(values.status === 'delivered' && values.bottles_returned != null
              ? { bottles_returned: values.bottles_returned }
              : {}),
          });
        }}>
          <Form.Item
            name="status"
            label={t('ui.orders.new_status', 'New Status')}
            rules={[{ required: true, message: t('ui.orders.select_status_required', 'Please select a status') }]}
            extra={
              allowedNextStatuses && allowedNextStatuses.size === 0
                ? t('ui.orders.no_valid_transitions', 'This order is in a terminal state and cannot be updated.')
                : undefined
            }
          >
            <Select
              disabled={allowedNextStatuses ? allowedNextStatuses.size === 0 : false}
              notFoundContent={t('ui.orders.no_valid_transitions', 'No valid transitions available.')}
            >
              {orderStatuses
                .filter((status) => (allowedNextStatuses ? allowedNextStatuses.has(status.value) : true))
                .map((status) => (
                  <Option key={status.value} value={status.value}>
                    {t(`ui.orders.status_${status.value}`, status.label)}
                  </Option>
                ))}
            </Select>
          </Form.Item>
          {watchedStatusValue === 'delivered' && (
            <Form.Item
              name="bottles_returned"
              label={t('ui.orders.bottles_returned', 'Bottles Returned')}
              extra={t('ui.orders.bottles_returned_hint', 'Number of returnable bottles collected from customer (optional)')}
            >
              <InputNumber min={0} style={{ width: '100%' }} placeholder="0" />
            </Form.Item>
          )}
          <Form.Item name="notes" label={t('ui.orders.notes_optional', 'Notes (Optional)')}>
            <Input.TextArea rows={3} placeholder={t('ui.orders.notes_placeholder', 'Notes')} />
          </Form.Item>
          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setIsStatusModalVisible(false)}>{t('ui.orders.close', 'Close')}</Button>
              <AsyncButton type="primary" htmlType="submit" loading={updateOrderMutation.isPending}>
                {t('ui.orders.update_status', 'Update Status')}
              </AsyncButton>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t('ui.orders.create_order', 'Create Order')}
        open={isCreateModalVisible}
        onCancel={createOrderReset}
        footer={null}
        width={760}
      >
        <Form form={createOrderForm} layout="vertical" onFinish={handleCreateOrderSubmit} initialValues={{ items: [{}], consume_marking_codes: false }}>
          {createOrderErrors.length > 0 ? (
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 16 }}
              message={t('ui.orders.order_create_validation_title', 'Could not create order')}
              description={
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {createOrderErrors.map((errorText) => (
                    <li key={errorText}>{errorText}</li>
                  ))}
                </ul>
              }
            />
          ) : null}

          <Form.Item
            name="user_id"
            label={t('ui.orders.select_customer', 'Select Customer')}
            rules={[{ required: true, message: t('ui.orders.customer_required', 'Please select a customer') }]}
          >
            <Select
              showSearch
              placeholder={t('ui.orders.search_customer', 'Search customer by name or phone')}
              filterOption={false}
              onSearch={handleUserSearch}
              onChange={handleUserSelect}
              loading={isUsersFetching}
              options={userOptions}
              notFoundContent={
                isUsersFetching
                  ? t('ui.common.searching', 'Searching...')
                  : userSearchTerm.length < 2
                    ? t('ui.orders.search_customer_hint', 'Type at least 2 characters to search')
                    : t('ui.orders.no_customers_found', 'No users found')
              }
            />
          </Form.Item>

          <Form.Item
            name="delivery_address_id"
            label={t('ui.orders.select_address', 'Select Delivery Address')}
            rules={[{ required: true, message: t('ui.orders.address_required', 'Please select a delivery address') }]}
          >
            <Select
              placeholder={
                selectedUserId
                  ? userAddresses.length > 0
                    ? t('ui.orders.select_address_placeholder', 'Select an address')
                    : t('ui.orders.no_addresses', 'No addresses found for this user')
                  : t('ui.orders.select_customer_first', 'Select a customer first')
              }
              disabled={!selectedUserId || userAddresses.length === 0}
            >
              {userAddresses.map((address) => (
                <Option key={address.id} value={address.id}>
                  {address.title ? `${address.title}: ` : ''}
                  {address.full_address}
                  {address.is_default ? ' (Default)' : ''}
                </Option>
              ))}
            </Select>
          </Form.Item>

          {selectedUserId && userAddresses.length === 0 ? (
            <div
              style={{
                background: '#fff7e6',
                border: '1px solid #ffd591',
                borderRadius: 6,
                padding: 12,
                marginBottom: 16,
              }}
            >
              <UserOutlined style={{ marginRight: 8 }} />
              {t('ui.orders.no_address_hint', 'This user has no saved addresses. Please add an address from the Users page first.')}
            </div>
          ) : null}

          {selectedUserId && paymentRestrictions?.cod_restricted ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 16 }}
              message={t('ui.orders.cod_restricted', 'Cash on delivery is restricted for this customer')}
              description={t(
                'ui.orders.cod_restricted_description',
                'This customer has reached the active COD debt limit. Use one of the prepaid methods below.',
              )}
            />
          ) : null}

          <Divider>{t('ui.orders.order_items', 'Order Items')}</Divider>
          <Form.List name="items">
            {(fields, { add, remove }) => (
              <>
                {fields.map(({ key, name, ...restField }) => (
                  <Row key={key} gutter={16} align="middle">
                    <Col span={14}>
                      <Form.Item
                        {...restField}
                        name={[name, 'product_id']}
                        rules={[{ required: true, message: t('ui.orders.product_required', 'Select product') }]}
                      >
                        <Select
                          showSearch
                          placeholder={t('ui.orders.select_product', 'Select product')}
                          optionFilterProp="children"
                          filterOption={(input, option) => String(option.children).toLowerCase().includes(input.toLowerCase())}
                        >
                          {(productsData?.data?.items || []).map((product) => {
                            const effectivePrice = getEffectiveProductPrice(product);
                            return (
                              <Option key={product.id} value={product.id}>
                                {product.name} - {formatMoney(effectivePrice)} UZS
                                {product.pricing_source === 'contract' ? ' (Contract)' : ''}
                              </Option>
                            );
                          })}
                        </Select>
                      </Form.Item>
                    </Col>
                    <Col span={6}>
                      <Form.Item
                        {...restField}
                        name={[name, 'quantity']}
                        rules={[{ required: true, message: t('ui.orders.quantity_required', 'Qty') }]}
                        initialValue={1}
                      >
                        <Select placeholder={t('ui.orders.quantity', 'Qty')}>
                          {Array.from({ length: 100 }, (_, index) => index + 1).map((value) => (
                            <Option key={value} value={value}>{value}</Option>
                          ))}
                        </Select>
                      </Form.Item>
                    </Col>
                    <Col span={4}>
                      {fields.length > 1 ? (
                        <Button type="text" danger icon={<MinusCircleOutlined />} onClick={() => remove(name)} />
                      ) : null}
                    </Col>
                  </Row>
                ))}
                <Form.Item>
                  <Button type="dashed" onClick={() => add()} block icon={<PlusOutlined />}>
                    {t('ui.orders.add_item', 'Add Item')}
                  </Button>
                </Form.Item>
              </>
            )}
          </Form.List>

          <Form.Item
            name="payment_method"
            label={t('ui.orders.payment_method', 'Payment Method')}
            rules={[{ required: true, message: t('ui.orders.payment_method_required', 'Please select a payment method') }]}
          >
            <Select
              loading={paymentMethodsLoading}
              disabled={!selectedUserId || userPaymentMethods.length === 0}
              placeholder={
                selectedUserId
                  ? t('ui.orders.select_payment_method', 'Select a payment method')
                  : t('ui.orders.select_customer_first', 'Select a customer first')
              }
            >
              {userPaymentMethods.map((method) => (
                <Option key={method.method} value={method.method}>
                  {t(`ui.orders.payment_${method.method}`, method.name || method.method)}
                </Option>
              ))}
            </Select>
          </Form.Item>

          {watchedPaymentMethod === 'business_account' ? (
            <Form.Item
              name="consume_marking_codes"
              label={t('ui.orders.consume_marking_codes', 'Consume Marking Codes')}
              valuePropName="checked"
              extra={t(
                'ui.orders.consume_marking_codes_help',
                'Leave disabled unless this business-account order should permanently consume product marking codes.',
              )}
            >
              <Switch />
            </Form.Item>
          ) : null}

          <Form.Item name="delivery_notes" label={t('ui.orders.delivery_notes', 'Delivery Notes')}>
            <Input.TextArea rows={2} placeholder={t('ui.orders.delivery_notes_placeholder', 'Any special delivery instructions...')} />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={createOrderReset}>{t('ui.common.cancel', 'Cancel')}</Button>
              <AsyncButton type="primary" htmlType="submit" loading={createOrderMutation.isPending} icon={<ShoppingCartOutlined />}>
                {t('ui.orders.create_order', 'Create Order')}
              </AsyncButton>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t('ui.orders.record_personal_card_payment', 'Record Personal Card Payment')}
        open={isPersonalCardModalVisible}
        onCancel={() => {
          setIsPersonalCardModalVisible(false);
          personalCardForm.resetFields();
        }}
        onOk={() => personalCardForm.submit()}
        confirmLoading={recordPersonalCardPaymentMutation.isPending}
      >
        <Form
          form={personalCardForm}
          layout="vertical"
          onFinish={(values) => {
            if (!selectedOrder?.id || !selectedOrder?.user_id) {
              message.error(t('ui.orders.order_context_missing', 'Order context is missing'));
              return;
            }
            recordPersonalCardPaymentMutation.mutate({
              customer_id: selectedOrder.user_id,
              order_id: selectedOrder.id,
              amount: values.amount,
              notes: values.notes,
              source: 'personal_card_transfer',
              proof_data: { channel: 'admin_ui_orders' },
            });
          }}
        >
          <Form.Item label={t('ui.orders.order_number', 'Order Number')}>
            <Input value={selectedOrder?.order_number} disabled />
          </Form.Item>
          <Form.Item label={t('ui.orders.outstanding_amount', 'Outstanding')}>
            <Input value={`${formatMoney(selectedOrder?.outstanding_amount)} UZS`} disabled />
          </Form.Item>
          <Form.Item name="amount" label={t('ui.orders.amount', 'Amount')} rules={[{ required: true, message: t('ui.orders.amount_required', 'Amount is required') }]}>
            <Input type="number" min={0} />
          </Form.Item>
          <Form.Item name="notes" label={t('ui.orders.notes', 'Notes')} rules={[{ required: true, message: t('ui.orders.notes_required', 'Notes are required') }]}>
            <Input.TextArea rows={3} placeholder={t('ui.orders.personal_card_notes_placeholder', 'Example: Customer transferred to owner personal card')} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={
          editItemsStep === 1
            ? `${t('ui.orders.edit_items', 'Edit Items')} — ${selectedOrder?.order_number || ''}`
            : `${t('ui.orders.edit_preview_title', 'Confirm Order Edit')} — ${selectedOrder?.order_number || ''}`
        }
        open={isEditItemsModalVisible}
        onCancel={handleCloseEditItems}
        footer={null}
        width={820}
        destroyOnClose
      >
        {editItemsStep === 1 ? (
          <Form form={editItemsForm} layout="vertical">
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message={t(
                'ui.orders.edit_items_hint',
                'Set the FINAL desired quantity per line. 0 removes a line. Add new rows to insert new line items.',
              )}
            />
            <Form.List name="items">
              {(fields, { add, remove }) => (
                <>
                  {fields.map(({ key, name, ...restField }) => (
                    <Row key={key} gutter={16} align="middle">
                      <Col span={2} style={{ textAlign: 'right', color: '#999' }}>#{name + 1}</Col>
                      <Col span={12}>
                        <Form.Item
                          {...restField}
                          name={[name, 'product_id']}
                          rules={[{ required: true, message: t('ui.orders.product_required', 'Select product') }]}
                        >
                          <Select
                            showSearch
                            placeholder={t('ui.orders.select_product', 'Select product')}
                            optionFilterProp="children"
                            filterOption={(input, option) => String(option.children).toLowerCase().includes(input.toLowerCase())}
                          >
                            {(productsData?.data?.items || []).map((product) => {
                              const effectivePrice = getEffectiveProductPrice(product);
                              return (
                                <Option key={product.id} value={product.id}>
                                  {product.name} - {formatMoney(effectivePrice)} UZS
                                </Option>
                              );
                            })}
                          </Select>
                        </Form.Item>
                      </Col>
                      <Col span={6}>
                        <Form.Item
                          {...restField}
                          name={[name, 'quantity']}
                          rules={[{ required: true, message: t('ui.orders.quantity_required', 'Qty') }]}
                        >
                          <InputNumber
                            min={0}
                            style={{ width: '100%' }}
                            placeholder={t('ui.orders.quantity', 'Qty')}
                          />
                        </Form.Item>
                      </Col>
                      <Col span={2}>
                        <Form.Item {...restField} name={[name, 'order_item_id']} hidden>
                          <Input />
                        </Form.Item>
                        <Button type="text" danger icon={<MinusCircleOutlined />} onClick={() => remove(name)} />
                      </Col>
                    </Row>
                  ))}
                  <Form.Item>
                    <Button type="dashed" onClick={() => add({ order_item_id: null })} block icon={<PlusOutlined />}>
                      {t('ui.orders.add_item', 'Add Item')}
                    </Button>
                  </Form.Item>
                </>
              )}
            </Form.List>

            <Form.Item
              name="reason"
              label={t('ui.orders.edit_reason', 'Reason')}
              rules={[
                { required: true, message: t('ui.orders.reason_required', 'Reason is required') },
                { min: 3, message: t('ui.orders.reason_min_length', 'Reason must be at least 3 characters') },
              ]}
            >
              <Input.TextArea
                rows={2}
                placeholder={t(
                  'ui.orders.reason_placeholder',
                  'Example: customer asked for 2 extra bottles on arrival',
                )}
              />
            </Form.Item>

            <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
              <Space>
                <Button onClick={handleCloseEditItems}>{t('ui.common.cancel', 'Cancel')}</Button>
                <AsyncButton type="primary" loading={editPreviewLoading} onClick={handleEditPreview}>
                  {t('ui.orders.preview_impacts', 'Preview impacts')}
                </AsyncButton>
              </Space>
            </Form.Item>
          </Form>
        ) : (
          <div>
            {(editPreviewData?.blocking_reasons || []).length ? (
              <Alert
                type="error"
                showIcon
                style={{ marginBottom: 16 }}
                message={t('ui.orders.edit_blocked', 'This edit cannot proceed')}
                description={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {editPreviewData.blocking_reasons.map((reason) => (
                      <li key={reason}>{reason}</li>
                    ))}
                  </ul>
                }
              />
            ) : null}

            {(editPreviewData?.warnings || []).length ? (
              <Alert
                type="warning"
                showIcon
                style={{ marginBottom: 16 }}
                message={t('ui.orders.edit_warnings', 'Warnings')}
                description={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {editPreviewData.warnings.map((warning) => (
                      <li key={warning}>{warning}</li>
                    ))}
                  </ul>
                }
              />
            ) : null}

            <Descriptions column={2} bordered size="small" style={{ marginBottom: 16 }}>
              <Descriptions.Item label={t('ui.orders.total_before', 'Total before')}>
                {formatMoney(editPreviewData?.totals_before?.total_amount)} UZS
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.total_after', 'Total after')}>
                <strong>
                  {formatMoney(editPreviewData?.totals_after?.total_amount)} UZS
                </strong>
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.subtotal_before', 'Subtotal before')}>
                {formatMoney(editPreviewData?.totals_before?.subtotal)} UZS
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.subtotal_after', 'Subtotal after')}>
                {formatMoney(editPreviewData?.totals_after?.subtotal)} UZS
              </Descriptions.Item>
            </Descriptions>

            <Divider>{t('ui.orders.cascade_impact', 'Cascade Impact')}</Divider>
            <Descriptions column={1} bordered size="small">
              <Descriptions.Item label={t('ui.orders.payment_impact', 'Payment')}>
                {(() => {
                  const summary = editPreviewData?.cascade_summary?.payment;
                  const action = summary?.action;
                  const originalMethod = summary?.payment_method_original;
                  const amount = summary?.prepayment_amount ?? summary?.additional_charge;
                  if (action === 'create_prepayment_credit') {
                    const cardSuffix =
                      originalMethod && originalMethod !== 'cash'
                        ? ` (card payment will NOT be refunded — credit is cash-only-usable)`
                        : '';
                    return t(
                      'ui.orders.payment_prepayment',
                      `Prepayment credit of ${formatMoney(amount)} UZS will be recorded${cardSuffix}`,
                    );
                  }
                  if (action === 'manual_cash_collection_required') {
                    return t(
                      'ui.orders.payment_extra_cash',
                      `Collect ${formatMoney(amount)} UZS extra in CASH via Personal Card Payment (card will not be re-charged)`,
                    );
                  }
                  if (action === 'totals_only') {
                    return t('ui.orders.payment_totals_only', 'Totals updated; no payment change');
                  }
                  return action || '—';
                })()}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.loyalty_impact', 'Loyalty')}>
                {(() => {
                  const loy = editPreviewData?.cascade_summary?.loyalty;
                  if (!loy) return '—';
                  return t(
                    'ui.orders.loyalty_change',
                    `${loy.old_points_earned || 0} AquaCoins → ${loy.new_points_earned || 0} AquaCoins`,
                  );
                })()}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.bottle_impact', 'Bottle balance')}>
                {(() => {
                  const bottle = editPreviewData?.cascade_summary?.bottle_balance;
                  if (!bottle || !(bottle.changes || []).length) {
                    return t('ui.orders.no_bottle_change', 'No bottle balance change');
                  }
                  return (
                    <div>
                      {(bottle.changes || []).map((change, idx) => (
                        <div key={idx}>
                          Product {change.product_id}: {change.delta_bottles > 0 ? '+' : ''}
                          {change.delta_bottles} {t('ui.orders.bottles', 'bottles')}
                        </div>
                      ))}
                      {bottle.affected_session_id ? (
                        <div style={{ color: '#fa8c16' }}>
                          {t(
                            'ui.orders.session_will_reopen',
                            `Driver bottle session #${bottle.affected_session_id} will be reopened`,
                          )}
                        </div>
                      ) : null}
                    </div>
                  );
                })()}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.corporate_impact', 'Corporate contract')}>
                {editPreviewData?.cascade_summary?.corporate?.manual_reconciliation_required
                  ? t('ui.orders.corporate_manual', 'Finance must reconcile contract ledger manually')
                  : editPreviewData?.cascade_summary?.corporate?.adjusted
                    ? t('ui.orders.corporate_adjusted', 'Contract reserve ledger will be adjusted')
                    : t('ui.orders.no_corporate_change', 'No corporate ledger change')}
              </Descriptions.Item>
            </Descriptions>

            <div style={{ marginTop: 16, textAlign: 'right' }}>
              <Space>
                <Button onClick={() => setEditItemsStep(1)}>
                  {t('ui.orders.back_to_edit', 'Back to edit')}
                </Button>
                <AsyncButton
                  type="primary"
                  loading={submitOrderEditMutation.isPending}
                  disabled={(editPreviewData?.blocking_reasons || []).length > 0}
                  onClick={handleEditConfirm}
                >
                  {t('ui.orders.confirm_apply', 'Confirm and apply')}
                </AsyncButton>
              </Space>
            </div>
          </div>
        )}
      </Modal>

      <Modal
        title={cashEditStep === 1
          ? `${t('ui.orders.edit_collected_cash', 'Edit collected cash')} — ${selectedOrder?.order_number || ''}`
          : `${t('ui.orders.collected_cash_confirm', 'Confirm cash correction')} — ${selectedOrder?.order_number || ''}`}
        open={isCashEditModalVisible}
        onCancel={() => setIsCashEditModalVisible(false)}
        footer={null}
        destroyOnClose
      >
        {cashEditStep === 1 ? (
          <Form form={cashEditForm} layout="vertical">
            <Alert type="info" showIcon style={{ marginBottom: 12 }}
              message={t('ui.orders.collected_cash_hint',
                'Enter the actual cash the driver collected. Any surplus over the order total becomes the customer\'s prepaid credit.')} />
            <Descriptions column={2} size="small" bordered style={{ marginBottom: 12 }}>
              <Descriptions.Item label={t('ui.orders.total_amount', 'Total Amount')}>
                {formatMoney(selectedOrder?.total_amount)} UZS
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.amount_collected', 'Collected')}>
                {formatMoney(selectedOrder?.amount_collected)} UZS
              </Descriptions.Item>
            </Descriptions>
            <Form.Item name="new_amount" label={t('ui.orders.new_collected_amount', 'Actual collected amount')}
              rules={[{ required: true, message: t('ui.orders.collected_cash_amount_required', 'Enter the collected amount') }]}>
              <InputNumber min={0} style={{ width: '100%' }} addonAfter="UZS" />
            </Form.Item>
            <Form.Item name="reason" label={t('ui.orders.collected_cash_reason', 'Reason')}
              rules={[{ required: true, min: 5, message: t('ui.orders.collected_cash_reason_required', 'Reason (min 5 chars) is required') }]}>
              <Input.TextArea rows={2} />
            </Form.Item>
            <div style={{ textAlign: 'right' }}>
              <Button onClick={() => setIsCashEditModalVisible(false)} style={{ marginRight: 8 }}>
                {t('ui.common.cancel', 'Cancel')}
              </Button>
              <Button type="primary" onClick={handleCashEditPreview}>
                {t('ui.orders.preview_impact', 'Preview impact')}
              </Button>
            </div>
          </Form>
        ) : (
          <div>
            <Descriptions column={1} size="small" bordered style={{ marginBottom: 12 }}>
              <Descriptions.Item label={t('ui.orders.new_collected_amount', 'Actual collected amount')}>
                {formatMoney(cashEditPreview?.new_amount)} UZS
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.surplus_or_shortfall', 'Surplus / shortfall')}>
                {formatMoney(cashEditPreview?.surplus_or_shortfall)} UZS
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.customer_credit', 'Customer credit change')}>
                {formatMoney(cashEditPreview?.customer_credit_delta)} UZS
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.cash_session_will_reopen', 'Driver session reopen')}>
                {cashEditPreview?.session_will_reopen ? t('ui.common.yes', 'Yes') : t('ui.common.no', 'No')}
              </Descriptions.Item>
            </Descriptions>
            {(cashEditPreview?.blocking_reasons || []).map((r, i) => (
              <Alert key={i} type="error" showIcon message={r} style={{ marginBottom: 6 }} />
            ))}
            {(cashEditPreview?.warnings || []).map((w, i) => (
              <Alert key={i} type="warning" showIcon message={w} style={{ marginBottom: 6 }} />
            ))}
            <div style={{ textAlign: 'right' }}>
              <Button onClick={() => setCashEditStep(1)} style={{ marginRight: 8 }}>
                {t('ui.common.back', 'Back')}
              </Button>
              <Button type="primary" loading={cashEditMutation.isPending} onClick={confirmCashEdit}
                disabled={(cashEditPreview?.blocking_reasons || []).length > 0}>
                {t('ui.orders.apply_correction', 'Apply correction')}
              </Button>
            </div>
          </div>
        )}
      </Modal>

      <Modal
        title={
          paymentMethodStep === 1
            ? `${t('ui.orders.edit_payment_method', 'Change')} — ${selectedOrder?.order_number || ''}`
            : `${t('ui.orders.payment_method_preview_title', 'Confirm Payment Method Change')} — ${selectedOrder?.order_number || ''}`
        }
        open={isPaymentMethodModalVisible}
        onCancel={handleClosePaymentMethodEdit}
        footer={null}
        destroyOnClose
      >
        {paymentMethodStep === 1 ? (
          <Form form={paymentMethodForm} layout="vertical">
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message={t(
                'ui.orders.payment_method_edit_hint',
                'Changing the payment method reconciles the corporate ledger and money side automatically. Preview the impact before applying.',
              )}
            />
            <Form.Item
              name="new_method"
              label={t('ui.orders.new_payment_method', 'New payment method')}
              rules={[{ required: true, message: t('ui.orders.payment_method_required', 'Please select a payment method') }]}
            >
              <Select placeholder={t('ui.orders.select_payment_method', 'Select a payment method')}>
                {(selectedOrder?.allowed_target_methods || []).map((method) => (
                  <Option key={method} value={method}>
                    {t(`ui.orders.payment_${method}`, method)}
                  </Option>
                ))}
              </Select>
            </Form.Item>
            <Form.Item
              name="reason"
              label={t('ui.orders.edit_reason', 'Reason')}
              rules={[
                { required: true, message: t('ui.orders.reason_required', 'Reason is required') },
                { min: 5, message: t('ui.orders.payment_method_reason_min_length', 'Reason must be at least 5 characters') },
              ]}
            >
              <Input.TextArea
                rows={2}
                placeholder={t(
                  'ui.orders.payment_method_reason_placeholder',
                  'Example: customer requested switch to corporate billing',
                )}
              />
            </Form.Item>

            <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
              <Space>
                <Button onClick={handleClosePaymentMethodEdit}>{t('ui.common.cancel', 'Cancel')}</Button>
                <AsyncButton type="primary" loading={paymentMethodPreviewLoading} onClick={handlePaymentMethodPreview}>
                  {t('ui.orders.preview_impacts', 'Preview impacts')}
                </AsyncButton>
              </Space>
            </Form.Item>
          </Form>
        ) : (
          <div>
            {(paymentMethodPreviewData?.blocking_reasons || []).length ? (
              <Alert
                type="error"
                showIcon
                style={{ marginBottom: 16 }}
                message={t('ui.orders.edit_blocked', 'This edit cannot proceed')}
                description={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {paymentMethodPreviewData.blocking_reasons.map((reason) => (
                      <li key={reason}>{reason}</li>
                    ))}
                  </ul>
                }
              />
            ) : null}

            {(paymentMethodPreviewData?.warnings || []).length ? (
              <Alert
                type="warning"
                showIcon
                style={{ marginBottom: 16 }}
                message={t('ui.orders.edit_warnings', 'Warnings')}
                description={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {paymentMethodPreviewData.warnings.map((warning) => (
                      <li key={warning}>{warning}</li>
                    ))}
                  </ul>
                }
              />
            ) : null}

            <Descriptions column={2} bordered size="small" style={{ marginBottom: 16 }}>
              <Descriptions.Item label={t('ui.orders.current_payment_method', 'Current method')}>
                {paymentMethodPreviewData?.current_method || '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.new_payment_method', 'New payment method')}>
                <strong>{paymentMethodPreviewData?.new_method || '—'}</strong>
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.orders.order_delivered', 'Order delivered')} span={2}>
                {paymentMethodPreviewData?.is_delivered ? t('ui.common.yes', 'Yes') : t('ui.common.no', 'No')}
              </Descriptions.Item>
            </Descriptions>

            <div style={{ marginTop: 16, textAlign: 'right' }}>
              <Space>
                <Button onClick={() => setPaymentMethodStep(1)}>
                  {t('ui.orders.back_to_edit', 'Back to edit')}
                </Button>
                <AsyncButton
                  type="primary"
                  loading={submitPaymentMethodMutation.isPending}
                  disabled={(paymentMethodPreviewData?.blocking_reasons || []).length > 0}
                  onClick={handlePaymentMethodConfirm}
                >
                  {t('ui.orders.confirm_apply', 'Confirm and apply')}
                </AsyncButton>
              </Space>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
};

export default Orders;
