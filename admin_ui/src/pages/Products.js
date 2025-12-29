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
  Avatar
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
  WarningOutlined
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient } from 'react-query';
import adminService from '../services/adminService';
import { useTranslation } from 'react-i18next';

const { Option } = Select;
const { TextArea } = Input;

const Products = () => {
  // Load products namespace for ui.products.* keys
  const { t } = useTranslation('products');
  const [searchText, setSearchText] = useState('');
  const [categoryFilter, setCategoryFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [selectedProduct, setSelectedProduct] = useState(null);
  const [isDetailModalVisible, setIsDetailModalVisible] = useState(false);
  const [isCreateModalVisible, setIsCreateModalVisible] = useState(false);
  const [isEditModalVisible, setIsEditModalVisible] = useState(false);
  const [pagination, setPagination] = useState({ page: 1, per_page: 20 });
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();
  const [createFileList, setCreateFileList] = useState([]);
  const [editFileList, setEditFileList] = useState([]);
  const [uploadingCreate, setUploadingCreate] = useState(false);
  const [uploadingEdit, setUploadingEdit] = useState(false);

  const queryClient = useQueryClient();

  // Fetch categories
  const { data: categoriesData } = useQuery(
    'categories',
    () => adminService.getCategories({ per_page: 100 }),
    {
      staleTime: 5 * 60 * 1000 // Cache for 5 minutes
    }
  );

  const categories = categoriesData?.data?.items || [];

  // Fetch products
  const { data, isLoading } = useQuery(
    ['products', pagination, searchText, categoryFilter, statusFilter],
    () => adminService.getProducts({
      page: pagination.page,
      per_page: pagination.per_page,
      search: searchText,
      category_id: categoryFilter,
      status: statusFilter
    }),
    {
      keepPreviousData: true
    }
  );

  // Create product mutation
  const createProductMutation = useMutation(
    (productData) => adminService.createProduct(productData),
    {
      onSuccess: () => {
        message.success(t('ui.products.created_success'));
        queryClient.invalidateQueries('products');
        setIsCreateModalVisible(false);
        createForm.resetFields();
        setCreateFileList([]);
      },
      onError: () => {
        message.error(t('ui.products.create_failed'));
      }
    }
  );

  // Update product mutation
  const updateProductMutation = useMutation(
    ({ productId, productData }) => adminService.updateProduct(productId, productData),
    {
      onSuccess: () => {
        message.success(t('ui.products.updated_success'));
        queryClient.invalidateQueries('products');
        setIsEditModalVisible(false);
        editForm.resetFields();
        setEditFileList([]);
      },
      onError: () => {
        message.error(t('ui.products.update_failed'));
      }
    }
  );

  // Delete product mutation
  const deleteProductMutation = useMutation(
    (productId) => adminService.deleteProduct(productId),
    {
      onSuccess: () => {
        message.success(t('ui.products.deleted_success'));
        queryClient.invalidateQueries('products');
      },
      onError: () => {
        message.error(t('ui.products.delete_failed'));
      }
    }
  );

  const productStatusColors = {
    active: 'green',
    inactive: 'red',
    out_of_stock: 'orange',
    discontinued: 'grey'
  };

  const columns = [
    {
      title: t('ui.products.image'),
      dataIndex: 'image_url',
      key: 'image',
      width: 80,
      render: (imageUrl, record) => (
        <Avatar
          shape="square"
          size={50}
          src={imageUrl}
          icon={<ShoppingOutlined />}
          alt={record.name}
        />
      )
    },
    {
      title: t('ui.products.product_name'),
      dataIndex: 'name',
      key: 'name',
      render: (text, record) => (
        <div>
          <div style={{ fontWeight: 'bold' }}>{text}</div>
          <small style={{ color: '#666' }}>{t('ui.products.sku')}: {record.sku}</small>
        </div>
      )
    },
    {
      title: t('ui.products.category'),
      dataIndex: 'category_id',
      key: 'category_id',
      width: 120,
      render: (category_id) => {
        const category = categories.find(c => c.id === category_id);
        return (
          <Tag color="blue">{category ? category.name : `ID: ${category_id}`}</Tag>
        );
      }
    },
    {
      title: t('ui.products.price'),
      dataIndex: 'price',
      key: 'price',
      width: 100,
      render: (price) => (
        <span style={{ fontWeight: 'bold', color: '#52c41a' }}>
          UZS{price?.toFixed(2)}
        </span>
      )
    },
    {
      title: t('ui.products.volume'),
      dataIndex: 'volume',
      key: 'volume',
      width: 100,
      render: (volume) => (
        <span style={{ fontWeight: 'bold', color: '#52c41a' }}>
          {volume}
        </span>
      )
    },
    {
      title: t('ui.products.stock'),
      dataIndex: 'stock_quantity',
      key: 'stock_quantity',
      width: 80,
      render: (stock) => (
        <span style={{
          color: stock <= 10 ? '#ff4d4f' : stock <= 50 ? '#faad14' : '#52c41a',
          fontWeight: 'bold'
        }}>
          {stock}
        </span>
      )
    },
    {
      title: t('ui.products.status'),
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (status) => (
        <Tag color={productStatusColors[status] || 'default'}>
          {t(`ui.products.status_${status}`)}
        </Tag>
      )
    },
    {
      title: t('ui.products.created'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 120,
      render: (date) => new Date(date).toLocaleDateString()
    },
    {
      title: t('ui.products.actions'),
      key: 'actions',
      width: 100,
      render: (_, record) => (
        <Dropdown
          menu={{
            items: [
              {
                key: 'view',
                label: t('ui.products.view_details'),
                icon: <EyeOutlined />,
                onClick: () => handleViewProduct(record)
              },
              {
                key: 'edit',
                label: t('ui.products.edit_product'),
                icon: <EditOutlined />,
                onClick: () => handleEditProduct(record)
              },
              {
                type: 'divider'
              },
              {
                key: 'delete',
                label: t('ui.products.delete_product'),
                icon: <DeleteOutlined />,
                danger: true,
                onClick: () => handleDeleteProduct(record)
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

  const handleViewProduct = (product) => {
    setSelectedProduct(product);
    setIsDetailModalVisible(true);
  };

  const handleEditProduct = (product) => {
    setSelectedProduct(product);
    editForm.setFieldsValue({
      name: product.name,
      description: product.description,
      category_id: product.category_id,
      price: product.price,
      stock_quantity: product.stock_quantity,
      volume: product.volume,
      status: product.status,
      is_featured: product.is_featured
    });
    // Set existing image in file list for preview
    if (product.image_url) {
      setEditFileList([{
        uid: '-1',
        name: 'current-image',
        status: 'done',
        url: product.image_url,
      }]);
    } else {
      setEditFileList([]);
    }
    setIsEditModalVisible(true);
  };

  const handleDeleteProduct = (product) => {
    Modal.confirm({
      title: t('ui.products.delete_product_title'),
      content: `${t('ui.products.delete_product_confirm')} "${product.name}"?`,
      icon: <WarningOutlined />,
      okText: t('ui.products.delete'),
      okType: 'danger',
      onOk: () => {
        deleteProductMutation.mutate(product.id);
      }
    });
  };

  const handleCreateSubmit = async (values) => {
    try {
      let imageUrl = null;

      // Upload image if one was selected
      if (createFileList.length > 0 && createFileList[0].originFileObj) {
        setUploadingCreate(true);
        const uploadResponse = await adminService.uploadImage(createFileList[0].originFileObj, {
          folder: 'products',
          resize: true,
          max_width: 800,
          max_height: 800
        });
        if (uploadResponse.success && uploadResponse.data?.url) {
          imageUrl = uploadResponse.data.url;
        }
        setUploadingCreate(false);
      }

      // Add image to product data
      const productData = {
        ...values,
        images: imageUrl ? [imageUrl] : []
      };

      createProductMutation.mutate(productData);
    } catch (error) {
      setUploadingCreate(false);
      message.error(t('ui.products.image_upload_failed'));
    }
  };

  const handleEditSubmit = async (values) => {
    try {
      let images = selectedProduct.images || [];

      // Upload new image if one was selected
      if (editFileList.length > 0 && editFileList[0].originFileObj) {
        setUploadingEdit(true);
        const uploadResponse = await adminService.uploadImage(editFileList[0].originFileObj, {
          folder: 'products',
          resize: true,
          max_width: 800,
          max_height: 800
        });
        if (uploadResponse.success && uploadResponse.data?.url) {
          images = [uploadResponse.data.url];
        }
        setUploadingEdit(false);
      } else if (editFileList.length === 0) {
        // Image was removed
        images = [];
      }

      const productData = {
        ...values,
        images: images
      };

      updateProductMutation.mutate({
        productId: selectedProduct.id,
        productData: productData
      });
    } catch (error) {
      setUploadingEdit(false);
      message.error(t('ui.products.image_upload_failed'));
    }
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

  const handleCategoryFilter = (value) => {
    setCategoryFilter(value);
    setPagination({ ...pagination, page: 1 });
  };

  const handleStatusFilter = (value) => {
    setStatusFilter(value);
    setPagination({ ...pagination, page: 1 });
  };

  // Calculate summary statistics
  const products = data?.data?.items || [];
  const totalProducts = data?.meta?.total || 0;
  const lowStockProducts = products.filter(product => product.stock_quantity <= 10).length;
  const totalValue = products.reduce((sum, product) => sum + (product.base_price * product.stock_quantity), 0);

  const createUploadProps = {
    name: 'file',
    multiple: false,
    listType: 'picture-card',
    fileList: createFileList,
    beforeUpload: (file) => {
      // Validate file type
      const isImage = file.type.startsWith('image/');
      if (!isImage) {
        message.error(t('ui.products.only_images_allowed'));
        return Upload.LIST_IGNORE;
      }
      // Validate file size (max 5MB)
      const isLt5M = file.size / 1024 / 1024 < 5;
      if (!isLt5M) {
        message.error(t('ui.products.image_too_large'));
        return Upload.LIST_IGNORE;
      }
      return false; // Prevent automatic upload
    },
    onChange({ fileList }) {
      setCreateFileList(fileList);
    },
    onRemove: () => {
      setCreateFileList([]);
    }
  };

  const editUploadProps = {
    name: 'file',
    multiple: false,
    listType: 'picture-card',
    fileList: editFileList,
    beforeUpload: (file) => {
      // Validate file type
      const isImage = file.type.startsWith('image/');
      if (!isImage) {
        message.error(t('ui.products.only_images_allowed'));
        return Upload.LIST_IGNORE;
      }
      // Validate file size (max 5MB)
      const isLt5M = file.size / 1024 / 1024 < 5;
      if (!isLt5M) {
        message.error(t('ui.products.image_too_large'));
        return Upload.LIST_IGNORE;
      }
      return false; // Prevent automatic upload
    },
    onChange({ fileList }) {
      setEditFileList(fileList);
    },
    onRemove: () => {
      setEditFileList([]);
    }
  };

  return (
    <div>
      {/* Summary Cards */}
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title={t('ui.products.total_products')}
              value={totalProducts}
              prefix={<ShoppingOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title={t('ui.products.low_stock_items')}
              value={lowStockProducts}
              valueStyle={{ color: lowStockProducts > 0 ? '#ff4d4f' : '#52c41a' }}
              prefix={<WarningOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title={t('ui.products.total_inventory_value')}
              value={totalValue}
              precision={2}
              prefix={<DollarOutlined />}
              suffix="UZS"
            />
          </Card>
        </Col>
      </Row>

      <Card>
        {/* Filter Controls */}
        <div className="table-actions">
          <Space wrap>
            <Input.Search
              placeholder={t('ui.products.search_placeholder')}
              allowClear
              onSearch={handleSearch}
              style={{ width: 250 }}
            />
            <Select
              placeholder={t('ui.products.filter_by_category')}
              allowClear
              onChange={handleCategoryFilter}
              style={{ width: 200 }}
              loading={!categoriesData}
            >
              {categories.filter(c => c.is_active).map(category => (
                <Option key={category.id} value={category.id}>
                  {category.name}
                </Option>
              ))}
            </Select>
            <Select
              placeholder={t('ui.products.filter_by_status')}
              allowClear
              onChange={handleStatusFilter}
              style={{ width: 150 }}
            >
              <Option value="active">{t('ui.products.status_active')}</Option>
              <Option value="inactive">{t('ui.products.status_inactive')}</Option>
              <Option value="out_of_stock">{t('ui.products.status_out_of_stock')}</Option>
              <Option value="discontinued">{t('ui.products.status_discontinued')}</Option>
            </Select>
          </Space>

          <Space>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => setIsCreateModalVisible(true)}
            >
              {t('ui.products.add_product')}
            </Button>
            <Button icon={<ExportOutlined />}>
              {t('ui.products.export_products')}
            </Button>
          </Space>
        </div>

        {/* Products Table */}
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
            showTotal: (total, range) =>
              `${range[0]}-${range[1]} of ${total} ${t('ui.products.pagination_text')}`
          }}
          onChange={handleTableChange}
          className="admin-table"
          scroll={{ x: 1000 }}
        />
      </Card>

      {/* Product Details Modal */}
      <Modal
        title={`${t('ui.products.product_details')} - ${selectedProduct?.name}`}
        open={isDetailModalVisible}
        onCancel={() => setIsDetailModalVisible(false)}
        footer={null}
        width={700}
      >
        {selectedProduct && (
          <div>
            <Row gutter={16}>
              <Col span={8}>
                <Image
                  width="100%"
                  src={selectedProduct.image_url}
                  alt={selectedProduct.name}
                  placeholder="No Image"
                />
              </Col>
              <Col span={16}>
                <h3>{selectedProduct.name}</h3>
                <p><strong>{t('ui.products.sku')}:</strong> {selectedProduct.sku}</p>
                <p><strong>{t('ui.products.category')}:</strong> {categories.find(c => c.id === selectedProduct.category_id)?.name || selectedProduct.category_id}</p>
                <p><strong>{t('ui.products.price')}:</strong> UZS{selectedProduct.price?.toFixed(2)}</p>
                <p><strong>{t('ui.products.volume')}:</strong> {selectedProduct.volume}</p>
                <p><strong>{t('ui.products.stock')}:</strong> {selectedProduct.stock_quantity} {t('ui.products.units')}</p>
                <p><strong>{t('ui.products.status')}:</strong>
                  <Tag color={productStatusColors[selectedProduct.status]} style={{ marginLeft: 8 }}>
                    {t(`ui.products.status_${selectedProduct.status}`)}
                  </Tag>
                </p>
                {selectedProduct.is_featured && (
                  <Tag color="gold">{t('ui.products.featured_product')}</Tag>
                )}
              </Col>
            </Row>

            <Divider>{t('ui.products.description')}</Divider>
            <p>{selectedProduct.description}</p>

            <div style={{ marginTop: 16, textAlign: 'right' }}>
              <Space>
                <Button
                  type="primary"
                  onClick={() => {
                    setIsDetailModalVisible(false);
                    handleEditProduct(selectedProduct);
                  }}
                >
                  {t('ui.products.edit_product')}
                </Button>
                <Button onClick={() => setIsDetailModalVisible(false)}>
                  {t('ui.products.close')}
                </Button>
              </Space>
            </div>
          </div>
        )}
      </Modal>

      {/* Create Product Modal */}
      <Modal
        title={t('ui.products.add_new_product')}
        open={isCreateModalVisible}
        onCancel={() => setIsCreateModalVisible(false)}
        footer={null}
        width={600}
      >
        <Form
          form={createForm}
          layout="vertical"
          onFinish={handleCreateSubmit}
        >
          <Form.Item
            name="name"
            label={t('ui.products.product_name_label')}
            rules={[{ required: true, message: t('ui.products.product_name_required') }]}
          >
            <Input placeholder={t('ui.products.product_name_placeholder')} />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
              name="sku"
              label={t('ui.products.sku_label')}
              rules={[{ required: true, message: t('ui.products.sku_required') }]}
            >
              <Input placeholder={t('ui.products.sku_placeholder')} />
            </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
              name="volume"
              label={t('ui.products.volume_label')}
              rules={[{ required: true, message: t('ui.products.volume_required') }]}
            >
              <InputNumber
                  placeholder={t('ui.products.volume_placeholder')}
                  style={{ width: '100%' }}
                  min={0}
                  precision={1}
                />
            </Form.Item>
            </Col>
          </Row>


          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="category_id"
                label={t('ui.products.category_label')}
                rules={[{ required: true, message: t('ui.products.category_required') }]}
              >
                <Select
                  placeholder={t('ui.products.category_placeholder')}
                  loading={!categoriesData}
                  notFoundContent={categories.length === 0 ? t('ui.products.no_categories') : "No categories"}
                >
                  {categories.filter(c => c.is_active).map(category => (
                    <Option key={category.id} value={category.id}>
                      {category.name}
                    </Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="status"
                label={t('ui.products.status_label')}
                rules={[{ required: true, message: t('ui.products.status_required') }]}
              >
                <Select placeholder={t('ui.products.status_placeholder')}>
                  <Option value="active">{t('ui.products.status_active')}</Option>
                  <Option value="inactive">{t('ui.products.status_inactive')}</Option>
                  <Option value="out_of_stock">{t('ui.products.status_out_of_stock')}</Option>
                  <Option value="discontinued">{t('ui.products.status_discontinued')}</Option>
                </Select>
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="price"
                label={t('ui.products.price_label')}
                rules={[{ required: true, message: t('ui.products.price_required') }]}
              >
                <InputNumber
                  placeholder="0.00"
                  prefix="UZS"
                  style={{ width: '100%' }}
                  min={0}
                  precision={2}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="stock_quantity"
                label={t('ui.products.stock_quantity_label')}
                rules={[{ required: true, message: t('ui.products.stock_quantity_required') }]}
              >
                <InputNumber
                  placeholder="0"
                  style={{ width: '100%' }}
                  min={0}
                />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item
            name="description"
            label={t('ui.products.description_label')}
          >
            <TextArea
              rows={3}
              placeholder={t('ui.products.description_placeholder')}
            />
          </Form.Item>

          <Form.Item
            name="is_featured"
            label={t('ui.products.featured_product_label')}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>

          <Form.Item
            label={t('ui.products.product_image_label')}
          >
            <Upload {...createUploadProps}>
              {createFileList.length < 1 && (
                <div>
                  <PlusOutlined />
                  <div style={{ marginTop: 8 }}>{t('ui.products.upload_image')}</div>
                </div>
              )}
            </Upload>
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => {
                setIsCreateModalVisible(false);
                setCreateFileList([]);
              }}>
                {t('ui.products.cancel')}
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={createProductMutation.isLoading || uploadingCreate}
              >
                {t('ui.products.create_product')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* Edit Product Modal */}
      <Modal
        title={`${t('ui.products.edit_product_title')} - ${selectedProduct?.name}`}
        open={isEditModalVisible}
        onCancel={() => setIsEditModalVisible(false)}
        footer={null}
        width={600}
      >
        <Form
          form={editForm}
          layout="vertical"
          onFinish={handleEditSubmit}
        >
          <Form.Item
            name="name"
            label={t('ui.products.product_name_label')}
            rules={[{ required: true, message: t('ui.products.product_name_required') }]}
          >
            <Input placeholder={t('ui.products.product_name_placeholder')} />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="category_id"
                label={t('ui.products.category_label')}
                rules={[{ required: true, message: t('ui.products.category_required') }]}
              >
                <Select
                  placeholder={t('ui.products.category_placeholder')}
                  loading={!categoriesData}
                  notFoundContent={categories.length === 0 ? t('ui.products.no_categories') : "No categories"}
                >
                  {categories.filter(c => c.is_active).map(category => (
                    <Option key={category.id} value={category.id}>
                      {category.name}
                    </Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="status"
                label={t('ui.products.status_label')}
                rules={[{ required: true, message: t('ui.products.status_required') }]}
              >
                <Select placeholder={t('ui.products.status_placeholder')}>
                  <Option value="active">{t('ui.products.status_active')}</Option>
                  <Option value="inactive">{t('ui.products.status_inactive')}</Option>
                  <Option value="out_of_stock">{t('ui.products.status_out_of_stock')}</Option>
                  <Option value="discontinued">{t('ui.products.status_discontinued')}</Option>
                </Select>
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="price"
                label={t('ui.products.price_label')}
                rules={[{ required: true, message: t('ui.products.price_required') }]}
              >
                <InputNumber
                  placeholder="0.00"
                  prefix="$"
                  style={{ width: '100%' }}
                  min={0}
                  precision={2}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="stock_quantity"
                label={t('ui.products.stock_quantity_label')}
                rules={[{ required: true, message: t('ui.products.stock_quantity_required') }]}
              >
                <InputNumber
                  placeholder="0"
                  style={{ width: '100%' }}
                  min={0}
                />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item
            name="description"
            label={t('ui.products.description_label')}
          >
            <TextArea
              rows={3}
              placeholder={t('ui.products.description_placeholder')}
            />
          </Form.Item>

          <Form.Item
            name="is_featured"
            label={t('ui.products.featured_product_label')}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>

          <Form.Item
            label={t('ui.products.product_image_label')}
          >
            <Upload {...editUploadProps}>
              {editFileList.length < 1 && (
                <div>
                  <PlusOutlined />
                  <div style={{ marginTop: 8 }}>{t('ui.products.upload_image')}</div>
                </div>
              )}
            </Upload>
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => {
                setIsEditModalVisible(false);
                setEditFileList([]);
              }}>
                {t('ui.products.cancel')}
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={updateProductMutation.isLoading || uploadingEdit}
              >
                {t('ui.products.update_product')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default Products;