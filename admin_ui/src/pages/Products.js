import { useState } from 'react';
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
  InputNumber,
  Row,
  Col,
  Statistic,
  message,
  Upload,
  Image,
  Switch,
  Divider,
  Avatar,
  Tabs,
  Alert,
  Descriptions,
} from 'antd';
import {
  ShoppingOutlined,
  MoreOutlined,
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  EyeOutlined,
  DollarOutlined,
  ExportOutlined,
  WarningOutlined,
  BarcodeOutlined,
  TagsOutlined,
  InboxOutlined,
  UploadOutlined,
  DownloadOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient } from 'react-query';
import adminService from '../services/adminService';
import { useTranslation } from 'react-i18next';
import { formatLocalDate } from '../utils/dateUtils';
import { extractApiErrorMessages } from '../utils/apiError';

const { Option } = Select;
const { TextArea } = Input;

const PRODUCT_STATUS_OPTIONS = ['active', 'inactive', 'out_of_stock', 'discontinued'];
const MARKING_CODE_STATUS_OPTIONS = ['available', 'reserved', 'used', 'archived'];
const MARKING_CODE_STATUS_LABELS = {
  available: 'Available',
  reserved: 'Reserved',
  used: 'Used',
  archived: 'Archived',
};

const getProductStatusColor = (status) => {
  switch (status) {
    case 'active':
      return 'green';
    case 'inactive':
      return 'red';
    case 'out_of_stock':
      return 'orange';
    case 'discontinued':
      return 'default';
    default:
      return 'default';
  }
};

const getMarkingCodeStatusColor = (status) => {
  switch (status) {
    case 'available':
      return 'green';
    case 'reserved':
      return 'orange';
    case 'used':
      return 'blue';
    case 'archived':
      return 'default';
    default:
      return 'default';
  }
};

const getMarkingCodeStatusLabel = (t, status) =>
  t(`ui.products.marking_code_status_${status}`, {
    defaultValue: MARKING_CODE_STATUS_LABELS[status] || status,
  });

const downloadBlob = (blob, filename) => {
  const url = window.URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  window.URL.revokeObjectURL(url);
};

const Products = () => {
  const { t } = useTranslation('products');
  const queryClient = useQueryClient();

  const [searchText, setSearchText] = useState('');
  const [categoryFilter, setCategoryFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [selectedProduct, setSelectedProduct] = useState(null);
  const [isDetailModalVisible, setIsDetailModalVisible] = useState(false);
  const [isCreateModalVisible, setIsCreateModalVisible] = useState(false);
  const [isEditModalVisible, setIsEditModalVisible] = useState(false);
  const [isCreateCodesModalVisible, setIsCreateCodesModalVisible] = useState(false);
  const [isEditCodeModalVisible, setIsEditCodeModalVisible] = useState(false);
  const [selectedMarkingCode, setSelectedMarkingCode] = useState(null);
  const [pagination, setPagination] = useState({ page: 1, per_page: 20 });
  const [markingCodesPagination, setMarkingCodesPagination] = useState({ page: 1, per_page: 25 });
  const [markingCodeSearch, setMarkingCodeSearch] = useState('');
  const [markingCodeStatusFilter, setMarkingCodeStatusFilter] = useState('');
  const [createFileList, setCreateFileList] = useState([]);
  const [editFileList, setEditFileList] = useState([]);
  const [csvFileList, setCsvFileList] = useState([]);
  const [uploadingCreate, setUploadingCreate] = useState(false);
  const [uploadingEdit, setUploadingEdit] = useState(false);

  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();
  const [createCodesForm] = Form.useForm();
  const [editCodeForm] = Form.useForm();

  const { data: categoriesData } = useQuery(
    'categories',
    () => adminService.getCategories({ per_page: 100 }),
    { staleTime: 5 * 60 * 1000 },
  );

  const categories = categoriesData?.data?.items || [];

  const { data, isLoading } = useQuery(
    ['products', pagination, searchText, categoryFilter, statusFilter],
    () =>
      adminService.getProducts({
        page: pagination.page,
        per_page: pagination.per_page,
        search: searchText,
        category_id: categoryFilter || undefined,
        status: statusFilter || undefined,
      }),
    { keepPreviousData: true },
  );

  const {
    data: markingCodesData,
    isLoading: isMarkingCodesLoading,
    refetch: refetchMarkingCodes,
  } = useQuery(
    ['product-marking-codes', selectedProduct?.id, markingCodesPagination, markingCodeSearch, markingCodeStatusFilter],
    () =>
      adminService.listProductMarkingCodes(selectedProduct.id, {
        page: markingCodesPagination.page,
        per_page: markingCodesPagination.per_page,
        search: markingCodeSearch,
        status: markingCodeStatusFilter || undefined,
      }),
    {
      enabled: Boolean(selectedProduct?.id && isDetailModalVisible),
      keepPreviousData: true,
    },
  );

  const syncSelectedProduct = (product) => {
    if (!product) {
      return;
    }
    setSelectedProduct(product);
    queryClient.setQueryData(['products', pagination, searchText, categoryFilter, statusFilter], (current) => {
      if (!current?.data?.items) {
        return current;
      }
      return {
        ...current,
        data: {
          ...current.data,
          items: current.data.items.map((item) => (item.id === product.id ? product : item)),
        },
      };
    });
  };

  const invalidateProductQueries = () => {
    queryClient.invalidateQueries('products');
    if (selectedProduct?.id) {
      queryClient.invalidateQueries(['product-marking-codes', selectedProduct.id]);
    }
  };

  const createProductMutation = useMutation((productData) => adminService.createProduct(productData), {
    onSuccess: () => {
      message.success(t('ui.products.created_success', 'Product created successfully'));
      queryClient.invalidateQueries('products');
      setIsCreateModalVisible(false);
      createForm.resetFields();
      setCreateFileList([]);
    },
    onError: (error) => {
      const errors = extractApiErrorMessages(error, t('ui.products.create_failed', 'Failed to create product'));
      message.error(errors[0]);
    },
  });

  const updateProductMutation = useMutation(
    ({ productId, productData }) => adminService.updateProduct(productId, productData),
    {
      onSuccess: (response) => {
        const updatedProduct = response?.data?.product;
        message.success(t('ui.products.updated_success', 'Product updated successfully'));
        invalidateProductQueries();
        if (updatedProduct) {
          syncSelectedProduct(updatedProduct);
        }
        setIsEditModalVisible(false);
        editForm.resetFields();
        setEditFileList([]);
      },
      onError: (error) => {
        const errors = extractApiErrorMessages(error, t('ui.products.update_failed', 'Failed to update product'));
        message.error(errors[0]);
      },
    },
  );

  const deleteProductMutation = useMutation((productId) => adminService.deleteProduct(productId), {
    onSuccess: () => {
      message.success(t('ui.products.deleted_success', 'Product deleted successfully'));
      queryClient.invalidateQueries('products');
    },
    onError: (error) => {
      const errors = extractApiErrorMessages(error, t('ui.products.delete_failed', 'Failed to delete product'));
      message.error(errors[0]);
    },
  });

  const createMarkingCodesMutation = useMutation(
    (payload) => adminService.createProductMarkingCodes(selectedProduct.id, payload),
    {
      onSuccess: (response) => {
        const createdCount = response?.data?.created || response?.data?.codes?.length || 0;
        message.success(
          t('ui.products.marking_codes_created', '{{count}} marking codes created', { count: createdCount }),
        );
        queryClient.invalidateQueries('products');
        queryClient.invalidateQueries(['product-marking-codes', selectedProduct.id]);
        createCodesForm.resetFields();
        setIsCreateCodesModalVisible(false);
      },
      onError: (error) => {
        const errors = extractApiErrorMessages(error, t('ui.products.marking_codes_create_failed', 'Failed to create marking codes'));
        message.error(errors[0]);
      },
    },
  );

  const updateMarkingCodeMutation = useMutation(
    ({ markingCodeId, payload }) => adminService.updateProductMarkingCode(selectedProduct.id, markingCodeId, payload),
    {
      onSuccess: () => {
        message.success(t('ui.products.marking_code_updated', 'Marking code updated successfully'));
        queryClient.invalidateQueries('products');
        queryClient.invalidateQueries(['product-marking-codes', selectedProduct.id]);
        editCodeForm.resetFields();
        setSelectedMarkingCode(null);
        setIsEditCodeModalVisible(false);
      },
      onError: (error) => {
        const errors = extractApiErrorMessages(error, t('ui.products.marking_code_update_failed', 'Failed to update marking code'));
        message.error(errors[0]);
      },
    },
  );

  const importMarkingCodesMutation = useMutation(
    (file) => adminService.importProductMarkingCodesCsv(selectedProduct.id, file),
    {
      onSuccess: (response) => {
        const payload = response?.data || {};
        message.success(
          t('ui.products.marking_codes_imported', '{{count}} marking codes imported', {
            count: payload.created || 0,
          }),
        );
        queryClient.invalidateQueries('products');
        queryClient.invalidateQueries(['product-marking-codes', selectedProduct.id]);
        setCsvFileList([]);

        if (payload.invalid_rows?.length) {
          Modal.info({
            title: t('ui.products.marking_code_import_issues', 'CSV import completed with issues'),
            width: 720,
            content: (
              <div style={{ maxHeight: 320, overflow: 'auto' }}>
                {payload.invalid_rows.map((row) => (
                  <div key={`${row.row || 'global'}-${row.reason || 'issue'}`} style={{ marginBottom: 12 }}>
                    <strong>{row.row ? `Row ${row.row}` : 'File issue'}:</strong> {row.reason}
                    {row.codes?.length ? <div>{row.codes.join(', ')}</div> : null}
                  </div>
                ))}
              </div>
            ),
          });
        }
      },
      onError: (error) => {
        const errors = extractApiErrorMessages(error, t('ui.products.marking_codes_import_failed', 'Failed to import marking codes'));
        message.error(errors[0]);
      },
    },
  );

  const products = data?.data?.items || [];
  const totalProducts = data?.meta?.total || 0;
  const lowStockProducts = products.filter((product) => (product.stock_quantity || 0) <= 10).length;
  const totalValue = products.reduce(
    (sum, product) => sum + Number((product.price ?? product.base_price ?? 0) * (product.stock_quantity || 0)),
    0,
  );
  const productsWithFiscalization = products.filter((product) => product.fiscalization_enabled).length;
  const lowMarkingCodeProducts = products.filter((product) => product.marking_codes_low_stock).length;

  const markingCodes = markingCodesData?.data?.items || [];
  const markingCodeSummary = markingCodesData?.data?.summary || selectedProduct?.marking_code_counts || {};

  const prepareProductData = (values) => {
    const {
      name_ru,
      description_ru,
      name_en,
      description_en,
      ...baseValues
    } = values;

    const dataPayload = { ...baseValues };
    const translations = {
      name: {},
      description: {},
    };

    if (name_ru) translations.name.ru = name_ru;
    if (name_en) translations.name.en = name_en;
    if (description_ru) translations.description.ru = description_ru;
    if (description_en) translations.description.en = description_en;

    if (Object.keys(translations.name).length || Object.keys(translations.description).length) {
      dataPayload.translations = translations;
    }

    return dataPayload;
  };

  const openProductEditModal = (product) => {
    setSelectedProduct(product);
    editForm.setFieldsValue({
      name: product.name,
      description: product.description,
      name_ru: product.name_translations?.ru || '',
      description_ru: product.description_translations?.ru || '',
      name_en: product.name_translations?.en || '',
      description_en: product.description_translations?.en || '',
      category_id: product.category_id,
      price: product.price,
      stock_quantity: product.stock_quantity,
      volume: product.volume,
      status: product.status,
      is_featured: product.is_featured,
      is_tryout_eligible: product.is_tryout_eligible !== false,
      tracks_returnable_bottles: Boolean(product.tracks_returnable_bottles),
      returnable_bottles_per_unit: product.returnable_bottles_per_unit || 0,
      barcode: product.barcode,
      spic: product.spic,
      package_code: product.package_code,
      units: product.units,
      vat_percent: product.vat_percent,
      fiscalization_enabled: Boolean(product.fiscalization_enabled),
      requires_marking_codes: Boolean(product.requires_marking_codes),
    });
    if (product.image_url) {
      setEditFileList([
        {
          uid: '-1',
          name: 'current-image',
          status: 'done',
          url: product.image_url,
        },
      ]);
    } else {
      setEditFileList([]);
    }
    setIsEditModalVisible(true);
  };

  const handleViewProduct = (product) => {
    setSelectedProduct(product);
    setMarkingCodesPagination({ page: 1, per_page: 25 });
    setMarkingCodeSearch('');
    setMarkingCodeStatusFilter('');
    setIsDetailModalVisible(true);
  };

  const handleDeleteProduct = (product) => {
    Modal.confirm({
      title: t('ui.products.delete_product_title', 'Delete product'),
      content: `${t('ui.products.delete_product_confirm', 'Delete product')} "${product.name}"?`,
      icon: <WarningOutlined />,
      okText: t('ui.products.delete', 'Delete'),
      okType: 'danger',
      onOk: () => deleteProductMutation.mutate(product.id),
    });
  };

  const handleCreateSubmit = async (values) => {
    try {
      let imageUrl = null;

      if (createFileList.length > 0 && createFileList[0].originFileObj) {
        setUploadingCreate(true);
        const uploadResponse = await adminService.uploadImage(createFileList[0].originFileObj, {
          folder: 'products',
          resize: true,
          max_width: 800,
          max_height: 800,
        });
        if (uploadResponse.success && uploadResponse.data?.url) {
          imageUrl = uploadResponse.data.url;
        }
      }

      createProductMutation.mutate({
        ...prepareProductData(values),
        images: imageUrl ? [imageUrl] : [],
      });
    } catch (error) {
      message.error(t('ui.products.image_upload_failed', 'Image upload failed'));
    } finally {
      setUploadingCreate(false);
    }
  };

  const handleEditSubmit = async (values) => {
    try {
      let images = selectedProduct?.images || [];

      if (editFileList.length > 0 && editFileList[0].originFileObj) {
        setUploadingEdit(true);
        const uploadResponse = await adminService.uploadImage(editFileList[0].originFileObj, {
          folder: 'products',
          resize: true,
          max_width: 800,
          max_height: 800,
        });
        if (uploadResponse.success && uploadResponse.data?.url) {
          images = [uploadResponse.data.url];
        }
      } else if (editFileList.length === 0) {
        images = [];
      }

      updateProductMutation.mutate({
        productId: selectedProduct.id,
        productData: {
          ...prepareProductData(values),
          images,
        },
      });
    } catch (error) {
      message.error(t('ui.products.image_upload_failed', 'Image upload failed'));
    } finally {
      setUploadingEdit(false);
    }
  };

  const handleCreateCodesSubmit = (values) => {
    const codes = (values.codes || '')
      .split(/\r?\n/)
      .map((item) => item.trim())
      .filter(Boolean);

    createMarkingCodesMutation.mutate({
      codes,
      notes: values.notes,
    });
  };

  const handleEditCode = (markingCode) => {
    setSelectedMarkingCode(markingCode);
    editCodeForm.setFieldsValue({
      code: markingCode.code,
      status: markingCode.status,
      notes: markingCode.notes,
    });
    setIsEditCodeModalVisible(true);
  };

  const handleArchiveCode = (markingCode) => {
    Modal.confirm({
      title: t('ui.products.archive_marking_code', 'Archive marking code'),
      content: markingCode.code,
      okText: t('ui.common.archive', 'Archive'),
      okType: 'danger',
      onOk: () =>
        updateMarkingCodeMutation.mutate({
          markingCodeId: markingCode.id,
          payload: { status: 'archived' },
        }),
    });
  };

  const handleRestoreCode = (markingCode) => {
    updateMarkingCodeMutation.mutate({
      markingCodeId: markingCode.id,
      payload: { status: 'available' },
    });
  };

  const handleImportCsv = () => {
    const file = csvFileList[0]?.originFileObj;
    if (!file) {
      message.warning(t('ui.products.select_csv_file', 'Select a CSV file first'));
      return;
    }
    importMarkingCodesMutation.mutate(file);
  };

  const handleExportCsv = async () => {
    if (!selectedProduct?.id) {
      return;
    }

    try {
      const blob = await adminService.exportProductMarkingCodes(selectedProduct.id, {
        status: markingCodeStatusFilter || undefined,
      });
      downloadBlob(blob, `product-${selectedProduct.id}-marking-codes.csv`);
    } catch (error) {
      const errors = extractApiErrorMessages(error, t('ui.products.export_failed', 'Export failed'));
      message.error(errors[0]);
    }
  };

  const uploadImageProps = (fileList, setFileList) => ({
    name: 'file',
    multiple: false,
    listType: 'picture-card',
    fileList,
    beforeUpload: (file) => {
      const isImage = file.type.startsWith('image/');
      if (!isImage) {
        message.error(t('ui.products.only_images_allowed', 'Only image files are allowed'));
        return Upload.LIST_IGNORE;
      }
      const isLt5M = file.size / 1024 / 1024 < 5;
      if (!isLt5M) {
        message.error(t('ui.products.image_too_large', 'Image is too large'));
        return Upload.LIST_IGNORE;
      }
      return false;
    },
    onChange: ({ fileList: nextFileList }) => setFileList(nextFileList),
    onRemove: () => setFileList([]),
  });

  const markingCodeUploadProps = {
    multiple: false,
    accept: '.csv,text/csv',
    fileList: csvFileList,
    beforeUpload: (file) => {
      const isCsv = file.type === 'text/csv' || file.name.toLowerCase().endsWith('.csv');
      if (!isCsv) {
        message.error(t('ui.products.only_csv_allowed', 'Only CSV files are allowed'));
        return Upload.LIST_IGNORE;
      }
      return false;
    },
    onChange: ({ fileList }) => setCsvFileList(fileList.slice(-1)),
    onRemove: () => setCsvFileList([]),
  };

  const columns = [
    {
      title: t('ui.products.image', 'Image'),
      dataIndex: 'image_url',
      key: 'image',
      width: 88,
      render: (imageUrl, record) => (
        <Avatar shape="square" size={52} src={imageUrl} icon={<ShoppingOutlined />} alt={record.name} />
      ),
    },
    {
      title: t('ui.products.product_name', 'Product'),
      dataIndex: 'name',
      key: 'name',
      render: (text, record) => (
        <div>
          <div style={{ fontWeight: 600 }}>{text}</div>
          <small style={{ color: '#666' }}>{t('ui.products.sku', 'SKU')}: {record.sku || '—'}</small>
        </div>
      ),
    },
    {
      title: t('ui.products.category', 'Category'),
      dataIndex: 'category_id',
      key: 'category_id',
      width: 150,
      render: (categoryId) => {
        const category = categories.find((item) => item.id === categoryId);
        return <Tag color="blue">{category?.name || `ID: ${categoryId}`}</Tag>;
      },
    },
    {
      title: t('ui.products.price', 'Price'),
      dataIndex: 'price',
      key: 'price',
      width: 120,
      render: (price) => <span style={{ fontWeight: 600, color: '#52c41a' }}>UZS {Number(price || 0).toLocaleString()}</span>,
    },
    {
      title: t('ui.products.stock', 'Stock'),
      dataIndex: 'stock_quantity',
      key: 'stock_quantity',
      width: 90,
      render: (stock) => (
        <span style={{ color: stock <= 10 ? '#ff4d4f' : stock <= 50 ? '#faad14' : '#52c41a', fontWeight: 600 }}>
          {stock ?? '—'}
        </span>
      ),
    },
    {
      title: t('ui.products.fiscal_profile', 'Fiscal'),
      key: 'fiscal',
      width: 220,
      render: (_, record) => (
        <Space direction="vertical" size={4}>
          <Space size={4} wrap>
            {record.fiscalization_enabled ? <Tag color="processing">{t('ui.products.fiscal_enabled', 'Fiscal enabled')}</Tag> : null}
            {record.requires_marking_codes ? <Tag color="purple">{t('ui.products.marked_product', 'Marked')}</Tag> : null}
            {record.marking_codes_low_stock ? <Tag color="error">{t('ui.products.low_marking_stock', 'Low labels')}</Tag> : null}
          </Space>
          {record.requires_marking_codes ? (
            <small style={{ color: '#666' }}>
              {t('ui.products.available_codes', 'Available codes')}: {record.marking_code_counts?.available || 0}
            </small>
          ) : (
            <small style={{ color: '#999' }}>{t('ui.products.no_marking_codes_required', 'No labels required')}</small>
          )}
        </Space>
      ),
    },
    {
      title: t('ui.products.status', 'Status'),
      dataIndex: 'status',
      key: 'status',
      width: 120,
      render: (status) => <Tag color={getProductStatusColor(status)}>{t(`ui.products.status_${status}`, status)}</Tag>,
    },
    {
      title: t('ui.products.created', 'Created'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 140,
      render: (date) => formatLocalDate(date),
    },
    {
      title: t('ui.products.actions', 'Actions'),
      key: 'actions',
      width: 100,
      render: (_, record) => (
        <Dropdown
          trigger={['click']}
          menu={{
            items: [
              {
                key: 'view',
                label: t('ui.products.view_details', 'View Details'),
                icon: <EyeOutlined />,
                onClick: () => handleViewProduct(record),
              },
              {
                key: 'edit',
                label: t('ui.products.edit_product', 'Edit Product'),
                icon: <EditOutlined />,
                onClick: () => openProductEditModal(record),
              },
              { type: 'divider' },
              {
                key: 'delete',
                label: t('ui.products.delete_product', 'Delete Product'),
                icon: <DeleteOutlined />,
                danger: true,
                onClick: () => handleDeleteProduct(record),
              },
            ],
          }}
        >
          <Button type="text" icon={<MoreOutlined />} />
        </Dropdown>
      ),
    },
  ];

  const markingCodeColumns = [
    {
      title: t('ui.products.marking_code', 'Marking Code'),
      dataIndex: 'code',
      key: 'code',
      render: (code) => <span style={{ fontFamily: 'monospace' }}>{code}</span>,
    },
    {
      title: t('ui.products.status', 'Status'),
      dataIndex: 'status',
      key: 'status',
      width: 130,
      render: (status) => (
        <Tag color={getMarkingCodeStatusColor(status)}>
          {getMarkingCodeStatusLabel(t, status)}
        </Tag>
      ),
    },
    {
      title: t('ui.products.notes', 'Notes'),
      dataIndex: 'notes',
      key: 'notes',
      render: (value) => value || '—',
    },
    {
      title: t('ui.products.created', 'Created'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 160,
      render: (value) => formatLocalDate(value),
    },
    {
      title: t('ui.products.used_at', 'Used'),
      dataIndex: 'used_at',
      key: 'used_at',
      width: 160,
      render: (value) => (value ? formatLocalDate(value) : '—'),
    },
    {
      title: t('ui.products.actions', 'Actions'),
      key: 'actions',
      width: 180,
      render: (_, record) => (
        <Space>
          <Button type="link" onClick={() => handleEditCode(record)}>
            {t('ui.products.edit', 'Edit')}
          </Button>
          {record.status === 'archived' ? (
            <Button type="link" onClick={() => handleRestoreCode(record)}>
              {t('ui.products.restore', 'Restore')}
            </Button>
          ) : (
            <Button
              type="link"
              danger
              disabled={record.status === 'reserved' || record.status === 'used'}
              onClick={() => handleArchiveCode(record)}
            >
              {t('ui.products.archive', 'Archive')}
            </Button>
          )}
        </Space>
      ),
    },
  ];

  const fiscalProfileContent = selectedProduct ? (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {selectedProduct.missing_required_fields?.length ? (
        <Alert
          type="warning"
          showIcon
          message={t('ui.products.missing_fiscal_fields', 'Fiscal profile is incomplete')}
          description={selectedProduct.missing_required_fields.join(', ')}
        />
      ) : null}

      {selectedProduct.marking_codes_low_stock ? (
        <Alert
          type="error"
          showIcon
          message={t('ui.products.marking_codes_low_stock_alert', 'Marking-code stock is below the operational threshold')}
          description={`${t('ui.products.available_codes', 'Available codes')}: ${markingCodeSummary.available || 0}`}
        />
      ) : null}

      <Descriptions bordered column={2} size="small">
        <Descriptions.Item label={t('ui.products.barcode', 'Barcode')}>
          {selectedProduct.barcode || '—'}
        </Descriptions.Item>
        <Descriptions.Item label={t('ui.products.spic', 'SPIC')}>
          {selectedProduct.spic || '—'}
        </Descriptions.Item>
        <Descriptions.Item label={t('ui.products.package_code', 'Package Code')}>
          {selectedProduct.package_code || '—'}
        </Descriptions.Item>
        <Descriptions.Item label={t('ui.products.units', 'Units')}>
          {selectedProduct.units || '—'}
        </Descriptions.Item>
        <Descriptions.Item label={t('ui.products.vat_percent', 'VAT %')}>
          {selectedProduct.vat_percent ?? 0}
        </Descriptions.Item>
        <Descriptions.Item label={t('ui.products.fiscalization_enabled', 'Fiscalization Enabled')}>
          <Tag color={selectedProduct.fiscalization_enabled ? 'processing' : 'default'}>
            {selectedProduct.fiscalization_enabled ? t('ui.common.enabled', 'Enabled') : t('ui.common.disabled', 'Disabled')}
          </Tag>
        </Descriptions.Item>
        <Descriptions.Item label={t('ui.products.requires_marking_codes', 'Requires Marking Codes')}>
          <Tag color={selectedProduct.requires_marking_codes ? 'purple' : 'default'}>
            {selectedProduct.requires_marking_codes ? t('ui.common.yes', 'Yes') : t('ui.common.no', 'No')}
          </Tag>
        </Descriptions.Item>
        <Descriptions.Item label={t('ui.products.marking_code_threshold', 'Low-Stock Threshold')}>
          {selectedProduct.marking_codes_low_stock_threshold || 0}
        </Descriptions.Item>
      </Descriptions>

      <Row gutter={[16, 16]}>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title={t('ui.products.available', 'Available')} value={markingCodeSummary.available || 0} prefix={<TagsOutlined />} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title={t('ui.products.reserved', 'Reserved')} value={markingCodeSummary.reserved || 0} prefix={<TagsOutlined />} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title={t('ui.products.used', 'Used')} value={markingCodeSummary.used || 0} prefix={<TagsOutlined />} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic title={t('ui.products.archived', 'Archived')} value={markingCodeSummary.archived || 0} prefix={<TagsOutlined />} />
          </Card>
        </Col>
      </Row>
    </Space>
  ) : null;

  const detailTabItems = selectedProduct
    ? [
        {
          key: 'overview',
          label: t('ui.products.overview', 'Overview'),
          children: (
            <div>
              <Row gutter={16}>
                <Col xs={24} md={8}>
                  <Image width="100%" src={selectedProduct.image_url} alt={selectedProduct.name} placeholder="No Image" />
                </Col>
                <Col xs={24} md={16}>
                  <Descriptions column={1} bordered size="small">
                    <Descriptions.Item label={t('ui.products.product_name', 'Product')}>{selectedProduct.name}</Descriptions.Item>
                    <Descriptions.Item label={t('ui.products.sku', 'SKU')}>{selectedProduct.sku || '—'}</Descriptions.Item>
                    <Descriptions.Item label={t('ui.products.category', 'Category')}>
                      {categories.find((category) => category.id === selectedProduct.category_id)?.name || selectedProduct.category_id}
                    </Descriptions.Item>
                    <Descriptions.Item label={t('ui.products.price', 'Price')}>
                      UZS {Number(selectedProduct.price || 0).toLocaleString()}
                    </Descriptions.Item>
                    <Descriptions.Item label={t('ui.products.volume', 'Volume')}>
                      {selectedProduct.volume || '—'} {selectedProduct.volume_unit || ''}
                    </Descriptions.Item>
                    <Descriptions.Item label={t('ui.products.stock', 'Stock')}>
                      {selectedProduct.stock_quantity ?? '—'}
                    </Descriptions.Item>
                    <Descriptions.Item label={t('ui.products.status', 'Status')}>
                      <Tag color={getProductStatusColor(selectedProduct.status)}>
                        {t(`ui.products.status_${selectedProduct.status}`, selectedProduct.status)}
                      </Tag>
                    </Descriptions.Item>
                  </Descriptions>
                </Col>
              </Row>
              <Divider>{t('ui.products.description', 'Description')}</Divider>
              <p>{selectedProduct.description || '—'}</p>
            </div>
          ),
        },
        {
          key: 'fiscal',
          label: t('ui.products.fiscal_profile', 'Fiscal Profile'),
          children: fiscalProfileContent,
        },
        {
          key: 'marking-codes',
          label: t('ui.products.marking_codes', 'Marking Codes'),
          children: (
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
              <div className="table-actions">
                <Space wrap>
                  <Input.Search
                    placeholder={t('ui.products.search_marking_codes', 'Search marking codes')}
                    allowClear
                    onSearch={(value) => {
                      setMarkingCodeSearch(value);
                      setMarkingCodesPagination((current) => ({ ...current, page: 1 }));
                    }}
                    style={{ width: 240 }}
                  />
                  <Select
                    allowClear
                    value={markingCodeStatusFilter || undefined}
                    onChange={(value) => {
                      setMarkingCodeStatusFilter(value || '');
                      setMarkingCodesPagination((current) => ({ ...current, page: 1 }));
                    }}
                    placeholder={t('ui.products.filter_marking_codes', 'Filter by status')}
                    style={{ width: 180 }}
                  >
                    {MARKING_CODE_STATUS_OPTIONS.map((status) => (
                      <Option key={status} value={status}>
                        {getMarkingCodeStatusLabel(t, status)}
                      </Option>
                    ))}
                  </Select>
                </Space>

                <Space wrap>
                  <Upload {...markingCodeUploadProps}>
                    <Button icon={<InboxOutlined />}>{t('ui.products.select_csv', 'Select CSV')}</Button>
                  </Upload>
                  <Button
                    icon={<UploadOutlined />}
                    onClick={handleImportCsv}
                    loading={importMarkingCodesMutation.isLoading}
                  >
                    {t('ui.products.import_csv', 'Import CSV')}
                  </Button>
                  <Button icon={<DownloadOutlined />} onClick={handleExportCsv}>
                    {t('ui.products.export_csv', 'Export CSV')}
                  </Button>
                  <Button icon={<ReloadOutlined />} onClick={() => refetchMarkingCodes()}>
                    {t('ui.common.refresh', 'Refresh')}
                  </Button>
                  <Button type="primary" icon={<PlusOutlined />} onClick={() => setIsCreateCodesModalVisible(true)}>
                    {t('ui.products.add_marking_codes', 'Add Codes')}
                  </Button>
                </Space>
              </div>

              <Table
                columns={markingCodeColumns}
                dataSource={markingCodes}
                loading={isMarkingCodesLoading}
                rowKey="id"
                pagination={{
                  current: markingCodesPagination.page,
                  pageSize: markingCodesPagination.per_page,
                  total: markingCodesData?.data?.total || 0,
                  showSizeChanger: true,
                  onChange: (page, pageSize) => {
                    setMarkingCodesPagination({ page, per_page: pageSize });
                  },
                }}
                size="small"
                scroll={{ x: 900 }}
              />
            </Space>
          ),
        },
      ]
    : [];

  const buildProductForm = (form, onFinish, fileList, setFileList, loading) => (
    <Form form={form} layout="vertical" onFinish={onFinish} initialValues={{ is_tryout_eligible: true }}>
      <Tabs
        defaultActiveKey="uz"
        items={[
          {
            key: 'uz',
            label: 'Uzbek (Default)',
            children: (
              <>
                <Form.Item
                  name="name"
                  label={t('ui.products.product_name_label', 'Product name')}
                  rules={[{ required: true, message: t('ui.products.product_name_required', 'Product name is required') }]}
                >
                  <Input placeholder={t('ui.products.product_name_placeholder', 'Product name')} />
                </Form.Item>
                <Form.Item name="description" label={t('ui.products.description_label', 'Description')}>
                  <TextArea rows={3} placeholder={t('ui.products.description_placeholder', 'Description')} />
                </Form.Item>
              </>
            ),
          },
          {
            key: 'ru',
            label: 'Russian',
            children: (
              <>
                <Form.Item name="name_ru" label={t('ui.products.product_name_ru', 'Product name (RU)')}>
                  <Input placeholder={t('ui.products.product_name_ru', 'Product name (RU)')} />
                </Form.Item>
                <Form.Item name="description_ru" label={t('ui.products.description_ru', 'Description (RU)')}>
                  <TextArea rows={3} placeholder={t('ui.products.description_ru', 'Description (RU)')} />
                </Form.Item>
              </>
            ),
          },
          {
            key: 'en',
            label: 'English',
            children: (
              <>
                <Form.Item name="name_en" label={t('ui.products.product_name_en', 'Product name (EN)')}>
                  <Input placeholder={t('ui.products.product_name_en', 'Product name (EN)')} />
                </Form.Item>
                <Form.Item name="description_en" label={t('ui.products.description_en', 'Description (EN)')}>
                  <TextArea rows={3} placeholder={t('ui.products.description_en', 'Description (EN)')} />
                </Form.Item>
              </>
            ),
          },
        ]}
      />

      <Divider orientation="left">{t('ui.products.product_basics', 'Product Basics')}</Divider>
      <Row gutter={16}>
        <Col span={12}>
          <Form.Item name="sku" label={t('ui.products.sku_label', 'SKU')} rules={[{ required: true, message: t('ui.products.sku_required', 'SKU is required') }]}>
            <Input placeholder={t('ui.products.sku_placeholder', 'SKU')} />
          </Form.Item>
        </Col>
        <Col span={12}>
          <Form.Item name="volume" label={t('ui.products.volume_label', 'Volume')} rules={[{ required: true, message: t('ui.products.volume_required', 'Volume is required') }]}>
            <InputNumber style={{ width: '100%' }} min={0} precision={1} />
          </Form.Item>
        </Col>
      </Row>

      <Row gutter={16}>
        <Col span={12}>
          <Form.Item name="category_id" label={t('ui.products.category_label', 'Category')} rules={[{ required: true, message: t('ui.products.category_required', 'Category is required') }]}>
            <Select placeholder={t('ui.products.category_placeholder', 'Select category')} loading={!categoriesData}>
              {categories.filter((category) => category.is_active).map((category) => (
                <Option key={category.id} value={category.id}>
                  {category.name}
                </Option>
              ))}
            </Select>
          </Form.Item>
        </Col>
        <Col span={12}>
          <Form.Item name="status" label={t('ui.products.status_label', 'Status')} rules={[{ required: true, message: t('ui.products.status_required', 'Status is required') }]}>
            <Select placeholder={t('ui.products.status_placeholder', 'Select status')}>
              {PRODUCT_STATUS_OPTIONS.map((status) => (
                <Option key={status} value={status}>
                  {t(`ui.products.status_${status}`, status)}
                </Option>
              ))}
            </Select>
          </Form.Item>
        </Col>
      </Row>

      <Row gutter={16}>
        <Col span={12}>
          <Form.Item name="price" label={t('ui.products.price_label', 'Price')} rules={[{ required: true, message: t('ui.products.price_required', 'Price is required') }]}>
            <InputNumber prefix="UZS" style={{ width: '100%' }} min={0} precision={2} />
          </Form.Item>
        </Col>
        <Col span={12}>
          <Form.Item name="stock_quantity" label={t('ui.products.stock_quantity_label', 'Stock Quantity')} rules={[{ required: true, message: t('ui.products.stock_quantity_required', 'Stock quantity is required') }]}>
            <InputNumber style={{ width: '100%' }} min={0} />
          </Form.Item>
        </Col>
      </Row>

      <Divider orientation="left">{t('ui.products.product_operations', 'Operational Settings')}</Divider>
      <Row gutter={16}>
        <Col span={8}>
          <Form.Item name="is_tryout_eligible" label={t('ui.products.is_tryout_eligible', 'Try-out Eligible')} valuePropName="checked">
            <Switch />
          </Form.Item>
        </Col>
        <Col span={8}>
          <Form.Item name="tracks_returnable_bottles" label={t('ui.products.tracks_returnable_bottles', 'Tracks Returnable Bottles')} valuePropName="checked">
            <Switch />
          </Form.Item>
        </Col>
        <Col span={8}>
          <Form.Item name="returnable_bottles_per_unit" label={t('ui.products.returnable_bottles_per_unit', 'Returnable Bottles Per Unit')}>
            <InputNumber style={{ width: '100%' }} min={0} precision={2} />
          </Form.Item>
        </Col>
      </Row>

      <Form.Item name="is_featured" label={t('ui.products.featured_product_label', 'Featured Product')} valuePropName="checked">
        <Switch />
      </Form.Item>

      <Divider orientation="left">{t('ui.products.fiscal_profile', 'Fiscal Profile')}</Divider>
      <Row gutter={16}>
        <Col span={12}>
          <Form.Item name="barcode" label={t('ui.products.barcode', 'Barcode')}>
            <Input prefix={<BarcodeOutlined />} placeholder={t('ui.products.barcode', 'Barcode')} />
          </Form.Item>
        </Col>
        <Col span={12}>
          <Form.Item name="spic" label={t('ui.products.spic', 'SPIC')}>
            <Input placeholder={t('ui.products.spic', 'SPIC')} />
          </Form.Item>
        </Col>
      </Row>
      <Row gutter={16}>
        <Col span={12}>
          <Form.Item name="package_code" label={t('ui.products.package_code', 'Package Code')}>
            <Input placeholder={t('ui.products.package_code', 'Package Code')} />
          </Form.Item>
        </Col>
        <Col span={12}>
          <Form.Item name="units" label={t('ui.products.units', 'Units')}>
            <Input placeholder={t('ui.products.units', 'Units')} />
          </Form.Item>
        </Col>
      </Row>
      <Row gutter={16}>
        <Col span={12}>
          <Form.Item name="vat_percent" label={t('ui.products.vat_percent', 'VAT %')}>
            <InputNumber style={{ width: '100%' }} min={0} max={100} precision={2} />
          </Form.Item>
        </Col>
        <Col span={12}>
          <Space direction="vertical" style={{ width: '100%', paddingTop: 8 }}>
            <Form.Item name="fiscalization_enabled" label={t('ui.products.fiscalization_enabled', 'Fiscalization Enabled')} valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="requires_marking_codes" label={t('ui.products.requires_marking_codes', 'Requires Marking Codes')} valuePropName="checked">
              <Switch />
            </Form.Item>
          </Space>
        </Col>
      </Row>

      <Form.Item label={t('ui.products.product_image_label', 'Product Image')}>
        <Upload {...uploadImageProps(fileList, setFileList)}>
          {fileList.length < 1 ? (
            <div>
              <PlusOutlined />
              <div style={{ marginTop: 8 }}>{t('ui.products.upload_image', 'Upload image')}</div>
            </div>
          ) : null}
        </Upload>
      </Form.Item>

      <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
        <Space>
          <Button
            onClick={() => {
              form.resetFields();
              setFileList([]);
              if (form === createForm) {
                setIsCreateModalVisible(false);
              } else {
                setIsEditModalVisible(false);
              }
            }}
          >
            {t('ui.common.cancel', 'Cancel')}
          </Button>
          <Button type="primary" htmlType="submit" loading={loading}>
            {form === createForm ? t('ui.products.create_product', 'Create Product') : t('ui.products.update_product', 'Update Product')}
          </Button>
        </Space>
      </Form.Item>
    </Form>
  );

  return (
    <div>
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={12} lg={6}>
          <Card>
            <Statistic title={t('ui.products.total_products', 'Total Products')} value={totalProducts} prefix={<ShoppingOutlined />} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card>
            <Statistic
              title={t('ui.products.low_stock_items', 'Low Stock Items')}
              value={lowStockProducts}
              valueStyle={{ color: lowStockProducts > 0 ? '#ff4d4f' : '#52c41a' }}
              prefix={<WarningOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card>
            <Statistic title={t('ui.products.fiscalized_products', 'Fiscalized Products')} value={productsWithFiscalization} prefix={<BarcodeOutlined />} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card>
            <Statistic
              title={t('ui.products.low_marking_stock_products', 'Low Marking-Code Stock')}
              value={lowMarkingCodeProducts}
              valueStyle={{ color: lowMarkingCodeProducts > 0 ? '#ff4d4f' : '#52c41a' }}
              prefix={<TagsOutlined />}
            />
          </Card>
        </Col>
      </Row>

      <Card style={{ marginBottom: 24 }}>
        <Statistic title={t('ui.products.total_inventory_value', 'Total Inventory Value')} value={totalValue} precision={2} prefix={<DollarOutlined />} suffix="UZS" />
      </Card>

      <Card>
        <div className="table-actions">
          <Space wrap>
            <Input.Search
              placeholder={t('ui.products.search_placeholder', 'Search products')}
              allowClear
              onSearch={(value) => {
                setSearchText(value);
                setPagination((current) => ({ ...current, page: 1 }));
              }}
              style={{ width: 250 }}
            />
            <Select
              placeholder={t('ui.products.filter_by_category', 'Filter by category')}
              allowClear
              onChange={(value) => {
                setCategoryFilter(value || '');
                setPagination((current) => ({ ...current, page: 1 }));
              }}
              style={{ width: 200 }}
              loading={!categoriesData}
            >
              {categories.filter((category) => category.is_active).map((category) => (
                <Option key={category.id} value={category.id}>
                  {category.name}
                </Option>
              ))}
            </Select>
            <Select
              placeholder={t('ui.products.filter_by_status', 'Filter by status')}
              allowClear
              onChange={(value) => {
                setStatusFilter(value || '');
                setPagination((current) => ({ ...current, page: 1 }));
              }}
              style={{ width: 180 }}
            >
              {PRODUCT_STATUS_OPTIONS.map((status) => (
                <Option key={status} value={status}>
                  {t(`ui.products.status_${status}`, status)}
                </Option>
              ))}
            </Select>
          </Space>

          <Space>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setIsCreateModalVisible(true)}>
              {t('ui.products.add_product', 'Add Product')}
            </Button>
            <Button icon={<ExportOutlined />} disabled>
              {t('ui.products.export_products', 'Export Products')}
            </Button>
          </Space>
        </div>

        <Table
          columns={columns}
          dataSource={products}
          loading={isLoading}
          rowKey="id"
          pagination={{
            current: pagination.page,
            pageSize: pagination.per_page,
            total: totalProducts,
            showSizeChanger: true,
            showQuickJumper: true,
            showTotal: (total, range) => `${range[0]}-${range[1]} of ${total} ${t('ui.products.pagination_text', 'products')}`,
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
        title={`${t('ui.products.product_details', 'Product Details')} - ${selectedProduct?.name || ''}`}
        open={isDetailModalVisible}
        onCancel={() => setIsDetailModalVisible(false)}
        footer={null}
        width={1120}
      >
        {selectedProduct ? (
          <>
            <Tabs items={detailTabItems} />
            <div style={{ marginTop: 16, textAlign: 'right' }}>
              <Space>
                <Button type="primary" onClick={() => {
                  setIsDetailModalVisible(false);
                  openProductEditModal(selectedProduct);
                }}>
                  {t('ui.products.edit_product', 'Edit Product')}
                </Button>
                <Button onClick={() => setIsDetailModalVisible(false)}>{t('ui.products.close', 'Close')}</Button>
              </Space>
            </div>
          </>
        ) : null}
      </Modal>

      <Modal
        title={t('ui.products.add_new_product', 'Add New Product')}
        open={isCreateModalVisible}
        onCancel={() => {
          setIsCreateModalVisible(false);
          createForm.resetFields();
          setCreateFileList([]);
        }}
        footer={null}
        width={760}
      >
        {buildProductForm(createForm, handleCreateSubmit, createFileList, setCreateFileList, createProductMutation.isLoading || uploadingCreate)}
      </Modal>

      <Modal
        title={`${t('ui.products.edit_product_title', 'Edit Product')} - ${selectedProduct?.name || ''}`}
        open={isEditModalVisible}
        onCancel={() => {
          setIsEditModalVisible(false);
          editForm.resetFields();
          setEditFileList([]);
        }}
        footer={null}
        width={760}
      >
        {buildProductForm(editForm, handleEditSubmit, editFileList, setEditFileList, updateProductMutation.isLoading || uploadingEdit)}
      </Modal>

      <Modal
        title={t('ui.products.add_marking_codes', 'Add Marking Codes')}
        open={isCreateCodesModalVisible}
        onCancel={() => {
          setIsCreateCodesModalVisible(false);
          createCodesForm.resetFields();
        }}
        footer={null}
      >
        <Form form={createCodesForm} layout="vertical" onFinish={handleCreateCodesSubmit}>
          <Form.Item
            name="codes"
            label={t('ui.products.marking_codes', 'Marking Codes')}
            rules={[{ required: true, message: t('ui.products.marking_codes_required', 'Enter at least one code') }]}
            extra={t('ui.products.marking_codes_help', 'Enter one code per line')}
          >
            <TextArea rows={8} placeholder={'000000000001\n000000000002'} />
          </Form.Item>
          <Form.Item name="notes" label={t('ui.products.notes', 'Notes')}>
            <TextArea rows={3} placeholder={t('ui.products.notes', 'Notes')} />
          </Form.Item>
          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setIsCreateCodesModalVisible(false)}>{t('ui.common.cancel', 'Cancel')}</Button>
              <Button type="primary" htmlType="submit" loading={createMarkingCodesMutation.isLoading}>
                {t('ui.products.create_marking_codes', 'Create Codes')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t('ui.products.edit_marking_code', 'Edit Marking Code')}
        open={isEditCodeModalVisible}
        onCancel={() => {
          setIsEditCodeModalVisible(false);
          setSelectedMarkingCode(null);
          editCodeForm.resetFields();
        }}
        footer={null}
      >
        <Form
          form={editCodeForm}
          layout="vertical"
          onFinish={(values) =>
            updateMarkingCodeMutation.mutate({
              markingCodeId: selectedMarkingCode.id,
              payload: values,
            })
          }
        >
          <Form.Item
            name="code"
            label={t('ui.products.marking_code', 'Marking Code')}
            rules={[{ required: true, message: t('ui.products.marking_code_required', 'Marking code is required') }]}
          >
            <Input />
          </Form.Item>
          <Form.Item name="status" label={t('ui.products.status', 'Status')}>
            <Select>
              {MARKING_CODE_STATUS_OPTIONS.map((status) => (
                <Option key={status} value={status}>
                  {getMarkingCodeStatusLabel(t, status)}
                </Option>
              ))}
            </Select>
          </Form.Item>
          <Form.Item name="notes" label={t('ui.products.notes', 'Notes')}>
            <TextArea rows={3} />
          </Form.Item>
          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setIsEditCodeModalVisible(false)}>{t('ui.common.cancel', 'Cancel')}</Button>
              <Button type="primary" htmlType="submit" loading={updateMarkingCodeMutation.isLoading}>
                {t('ui.common.save', 'Save')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default Products;
