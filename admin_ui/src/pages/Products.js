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
  SearchOutlined,
  ShoppingOutlined,
  MoreOutlined,
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  EyeOutlined,
  InboxOutlined,
  DollarOutlined,
  ExportOutlined,
  TagsOutlined,
  WarningOutlined
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient } from 'react-query';
import adminService from '../services/adminService';

const { Option } = Select;
const { TextArea } = Input;
const { Dragger } = Upload;

const Products = () => {
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
        message.success('Product created successfully');
        queryClient.invalidateQueries('products');
        setIsCreateModalVisible(false);
        createForm.resetFields();
      },
      onError: (error) => {
        message.error('Failed to create product');
      }
    }
  );

  // Update product mutation
  const updateProductMutation = useMutation(
    ({ productId, productData }) => adminService.updateProduct(productId, productData),
    {
      onSuccess: () => {
        message.success('Product updated successfully');
        queryClient.invalidateQueries('products');
        setIsEditModalVisible(false);
        editForm.resetFields();
      },
      onError: (error) => {
        message.error('Failed to update product');
      }
    }
  );

  // Delete product mutation
  const deleteProductMutation = useMutation(
    (productId) => adminService.deleteProduct(productId),
    {
      onSuccess: () => {
        message.success('Product deleted successfully');
        queryClient.invalidateQueries('products');
      },
      onError: (error) => {
        message.error('Failed to delete product');
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
      title: 'Image',
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
      title: 'Product Name',
      dataIndex: 'name',
      key: 'name',
      render: (text, record) => (
        <div>
          <div style={{ fontWeight: 'bold' }}>{text}</div>
          <small style={{ color: '#666' }}>SKU: {record.sku}</small>
        </div>
      )
    },
    {
      title: 'Category',
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
      title: 'Price',
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
      title: 'Volume',
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
      title: 'Stock',
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
      title: 'Status',
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (status) => (
        <Tag color={productStatusColors[status] || 'default'}>
          {status?.toUpperCase().replace('_', ' ')}
        </Tag>
      )
    },
    {
      title: 'Created',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 120,
      render: (date) => new Date(date).toLocaleDateString()
    },
    {
      title: 'Actions',
      key: 'actions',
      width: 100,
      render: (_, record) => (
        <Dropdown
          menu={{
            items: [
              {
                key: 'view',
                label: 'View Details',
                icon: <EyeOutlined />,
                onClick: () => handleViewProduct(record)
              },
              {
                key: 'edit',
                label: 'Edit Product',
                icon: <EditOutlined />,
                onClick: () => handleEditProduct(record)
              },
              {
                type: 'divider'
              },
              {
                key: 'delete',
                label: 'Delete Product',
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
    setIsEditModalVisible(true);
  };

  const handleDeleteProduct = (product) => {
    Modal.confirm({
      title: 'Delete Product?',
      content: `Are you sure you want to delete "${product.name}"?`,
      icon: <WarningOutlined />,
      okText: 'Delete',
      okType: 'danger',
      onOk: () => {
        deleteProductMutation.mutate(product.id);
      }
    });
  };

  const handleCreateSubmit = (values) => {
    createProductMutation.mutate(values);
  };

  const handleEditSubmit = (values) => {
    updateProductMutation.mutate({
      productId: selectedProduct.id,
      productData: values
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

  const uploadProps = {
    name: 'file',
    multiple: false,
    beforeUpload: () => false, // Prevent automatic upload
    onChange(info) {
      const { status } = info.file;
      if (status === 'done') {
        message.success(`${info.file.name} file uploaded successfully.`);
      } else if (status === 'error') {
        message.error(`${info.file.name} file upload failed.`);
      }
    }
  };

  return (
    <div>
      {/* Summary Cards */}
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title="Total Products"
              value={totalProducts}
              prefix={<ShoppingOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title="Low Stock Items"
              value={lowStockProducts}
              valueStyle={{ color: lowStockProducts > 0 ? '#ff4d4f' : '#52c41a' }}
              prefix={<WarningOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title="Total Inventory Value"
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
              placeholder="Search products..."
              allowClear
              onSearch={handleSearch}
              style={{ width: 250 }}
            />
            <Select
              placeholder="Filter by category"
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
              placeholder="Filter by status"
              allowClear
              onChange={handleStatusFilter}
              style={{ width: 150 }}
            >
              <Option value="active">Active</Option>
              <Option value="inactive">Inactive</Option>
              <Option value="out_of_stock">Out of Stock</Option>
              <Option value="discontinued">Discontinued</Option>
            </Select>
          </Space>

          <Space>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => setIsCreateModalVisible(true)}
            >
              Add Product
            </Button>
            <Button icon={<ExportOutlined />}>
              Export Products
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
              `${range[0]}-${range[1]} of ${total} products`
          }}
          onChange={handleTableChange}
          className="admin-table"
          scroll={{ x: 1000 }}
        />
      </Card>

      {/* Product Details Modal */}
      <Modal
        title={`Product Details - ${selectedProduct?.name}`}
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
                <p><strong>SKU:</strong> {selectedProduct.sku}</p>
                <p><strong>Category:</strong> {categories.find(c => c.id === selectedProduct.category_id)?.name || selectedProduct.category_id}</p>
                <p><strong>Price:</strong> UZS{selectedProduct.price?.toFixed(2)}</p>
                <p><strong>Volume:</strong> {selectedProduct.volume}</p>
                <p><strong>Stock:</strong> {selectedProduct.stock_quantity} units</p>
                <p><strong>Status:</strong>
                  <Tag color={productStatusColors[selectedProduct.status]} style={{ marginLeft: 8 }}>
                    {selectedProduct.status?.toUpperCase().replace('_', ' ')}
                  </Tag>
                </p>
                {selectedProduct.is_featured && (
                  <Tag color="gold">Featured Product</Tag>
                )}
              </Col>
            </Row>

            <Divider>Description</Divider>
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
                  Edit Product
                </Button>
                <Button onClick={() => setIsDetailModalVisible(false)}>
                  Close
                </Button>
              </Space>
            </div>
          </div>
        )}
      </Modal>

      {/* Create Product Modal */}
      <Modal
        title="Add New Product"
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
            label="Product Name"
            rules={[{ required: true, message: 'Please enter product name' }]}
          >
            <Input placeholder="Enter product name" />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
              name="sku"
              label="SKU"
              rules={[{ required: true, message: 'Please enter SKU' }]}
            >
              <Input placeholder="Enter SKU" />
            </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
              name="volume"
              label="Volume"
              rules={[{ required: true, message: 'Please enter Volume' }]}
            >
              <InputNumber
                  placeholder="Enter Volume"
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
                label="Category"
                rules={[{ required: true, message: 'Please select category' }]}
              >
                <Select
                  placeholder="Select category"
                  loading={!categoriesData}
                  notFoundContent={categories.length === 0 ? "No categories found. Please create a category first." : "No categories"}
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
                label="Status"
                rules={[{ required: true, message: 'Please select status' }]}
              >
                <Select placeholder="Select status">
                  <Option value="active">Active</Option>
                  <Option value="inactive">Inactive</Option>
                  <Option value="out_of_stock">Out of Stock</Option>
                  <Option value="discontinued">Discontinued</Option>
                </Select>
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="price"
                label="Price"
                rules={[{ required: true, message: 'Please enter price' }]}
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
                label="Stock Quantity"
                rules={[{ required: true, message: 'Please enter stock quantity' }]}
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
            label="Description"
          >
            <TextArea
              rows={3}
              placeholder="Enter product description..."
            />
          </Form.Item>

          <Form.Item
            name="is_featured"
            label="Featured Product"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>

          <Form.Item
            name="image"
            label="Product Image"
          >
            <Dragger {...uploadProps}>
              <p className="ant-upload-drag-icon">
                <InboxOutlined />
              </p>
              <p className="ant-upload-text">Click or drag image to upload</p>
              <p className="ant-upload-hint">Support for single image upload. JPG, PNG files only.</p>
            </Dragger>
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setIsCreateModalVisible(false)}>
                Cancel
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={createProductMutation.isLoading}
              >
                Create Product
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* Edit Product Modal */}
      <Modal
        title={`Edit Product - ${selectedProduct?.name}`}
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
            label="Product Name"
            rules={[{ required: true, message: 'Please enter product name' }]}
          >
            <Input placeholder="Enter product name" />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="category_id"
                label="Category"
                rules={[{ required: true, message: 'Please select category' }]}
              >
                <Select
                  placeholder="Select category"
                  loading={!categoriesData}
                  notFoundContent={categories.length === 0 ? "No categories found. Please create a category first." : "No categories"}
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
                label="Status"
                rules={[{ required: true, message: 'Please select status' }]}
              >
                <Select placeholder="Select status">
                  <Option value="active">Active</Option>
                  <Option value="inactive">Inactive</Option>
                  <Option value="out_of_stock">Out of Stock</Option>
                  <Option value="discontinued">Discontinued</Option>
                </Select>
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="price"
                label="Price"
                rules={[{ required: true, message: 'Please enter price' }]}
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
                label="Stock Quantity"
                rules={[{ required: true, message: 'Please enter stock quantity' }]}
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
            label="Description"
          >
            <TextArea
              rows={3}
              placeholder="Enter product description..."
            />
          </Form.Item>

          <Form.Item
            name="is_featured"
            label="Featured Product"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setIsEditModalVisible(false)}>
                Cancel
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={updateProductMutation.isLoading}
              >
                Update Product
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default Products;