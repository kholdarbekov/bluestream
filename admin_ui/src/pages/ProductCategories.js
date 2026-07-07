import React, { useState } from 'react';
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
  InputNumber,
  Row,
  Col,
  Statistic,
  message,
  Switch,
  Divider,
  Tabs
} from 'antd';
import {
  SearchOutlined,
  TagsOutlined,
  MoreOutlined,
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  EyeOutlined,
  WarningOutlined,
  SortAscendingOutlined
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import adminService from '../services/adminService';
import { formatLocalDate } from '../utils/dateUtils';

const { TextArea } = Input;

const ProductCategories = () => {
  const { t } = useTranslation('product_categories');
  const [searchText, setSearchText] = useState('');
  const [selectedCategory, setSelectedCategory] = useState(null);
  const [isDetailModalVisible, setIsDetailModalVisible] = useState(false);
  const [isCreateModalVisible, setIsCreateModalVisible] = useState(false);
  const [isEditModalVisible, setIsEditModalVisible] = useState(false);
  const [pagination, setPagination] = useState({ page: 1, per_page: DEFAULT_PAGE_SIZE });
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();

  const queryClient = useQueryClient();

  // Fetch categories
  const { data, isLoading } = useQuery({
    queryKey: ['categories', pagination, searchText],

    queryFn: () => adminService.getCategories({
      page: pagination.page,
      per_page: pagination.per_page,
      search: searchText
    }),

    placeholderData: keepPreviousData,
  });

  // Create category mutation
  const createCategoryMutation = useMutation({
    mutationFn: (categoryData) => adminService.createCategory(categoryData),

    onSuccess: () => {
      message.success(t('created_toast', { defaultValue: 'Category created successfully' }));
      queryClient.invalidateQueries({
        queryKey: ['categories'],
      });
      setIsCreateModalVisible(false);
      createForm.resetFields();
    },

    onError: (error) => {
      const errorMessage = error.response?.data?.message || t('create_failed', { defaultValue: 'Failed to create category' });
      message.error(errorMessage);
    },
  });

  // Update category mutation
  const updateCategoryMutation = useMutation({
    mutationFn: ({ categoryId, categoryData }) => adminService.updateCategory(categoryId, categoryData),

    onSuccess: () => {
      message.success(t('updated_toast', { defaultValue: 'Category updated successfully' }));
      queryClient.invalidateQueries({
        queryKey: ['categories'],
      });
      setIsEditModalVisible(false);
      editForm.resetFields();
    },

    onError: (error) => {
      const errorMessage = error.response?.data?.message || t('update_failed', { defaultValue: 'Failed to update category' });
      message.error(errorMessage);
    },
  });

  // Delete category mutation
  const deleteCategoryMutation = useMutation({
    mutationFn: ({ categoryId, force }) => adminService.deleteCategory(categoryId, force),

    onSuccess: () => {
      message.success(t('deleted_toast', { defaultValue: 'Category deleted successfully' }));
      queryClient.invalidateQueries({
        queryKey: ['categories'],
      });
    },

    onError: (error) => {
      const errorMessage = error.response?.data?.message || t('delete_failed', { defaultValue: 'Failed to delete category' });
      message.error(errorMessage);
    },
  });

  const columns = [
    {
      title: t('id', { defaultValue: 'ID' }),
      dataIndex: 'id',
      key: 'id',
      width: 70,
      sorter: (a, b) => a.id - b.id
    },
    {
      title: t('name', { defaultValue: 'Name' }),
      dataIndex: 'name',
      key: 'name',
      render: (text, record) => (
        <div>
          <div style={{ fontWeight: 'bold' }}>{text}</div>
          {record.description && (
            <small style={{ color: '#666' }}>{record.description.substring(0, 50)}...</small>
          )}
        </div>
      ),
      sorter: (a, b) => a.name.localeCompare(b.name)
    },
    {
      title: t('sort_order', { defaultValue: 'Sort Order' }),
      dataIndex: 'sort_order',
      key: 'sort_order',
      width: 100,
      align: 'center',
      sorter: (a, b) => a.sort_order - b.sort_order
    },
    {
      title: t('products', { defaultValue: 'Products' }),
      dataIndex: 'product_count',
      key: 'product_count',
      width: 100,
      align: 'center',
      render: (count) => (
        <Tag color={count > 0 ? 'blue' : 'default'}>
          {count} {count === 1 ? t('product_singular', { defaultValue: 'product' }) : t('product_plural', { defaultValue: 'products' })}
        </Tag>
      ),
      sorter: (a, b) => a.product_count - b.product_count
    },
    {
      title: t('status', { defaultValue: 'Status' }),
      dataIndex: 'is_active',
      key: 'is_active',
      width: 100,
      render: (is_active) => (
        <Tag color={is_active ? 'green' : 'red'}>
          {is_active ? t('active', { defaultValue: 'Active' }) : t('inactive', { defaultValue: 'Inactive' })}
        </Tag>
      )
    },
    {
      title: t('created', { defaultValue: 'Created' }),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 120,
      render: (date) => formatLocalDate(date)
    },
    {
      title: t('actions', { defaultValue: 'Actions' }),
      key: 'actions',
      width: 100,
      render: (_, record) => (
        <Dropdown
          menu={{
            items: [
              {
                key: 'view',
                label: t('view_details', { defaultValue: 'View Details' }),
                icon: <EyeOutlined />,
                // eslint-disable-next-line no-use-before-define
                onClick: () => handleViewCategory(record)
              },
              {
                key: 'edit',
                label: t('edit_category', { defaultValue: 'Edit Category' }),
                icon: <EditOutlined />,
                // eslint-disable-next-line no-use-before-define
                onClick: () => handleEditCategory(record)
              },
              {
                type: 'divider'
              },
              {
                key: 'delete',
                label: t('delete_category', { defaultValue: 'Delete Category' }),
                icon: <DeleteOutlined />,
                danger: true,
                // eslint-disable-next-line no-use-before-define
                onClick: () => handleDeleteCategory(record)
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

  const handleViewCategory = (category) => {
    setSelectedCategory(category);
    setIsDetailModalVisible(true);
  };

  const handleEditCategory = (category) => {
    setSelectedCategory(category);
    editForm.setFieldsValue({
      name: category.name,
      description: category.description,

      // Russian translations
      name_ru: category.name_translations?.ru || '',
      description_ru: category.description_translations?.ru || '',

      // English translations
      name_en: category.name_translations?.en || '',
      description_en: category.description_translations?.en || '',

      sort_order: category.sort_order,
      icon_url: category.icon_url,
      is_active: category.is_active
    });
    setIsEditModalVisible(true);
  };

  const handleDeleteCategory = (category) => {
    const hasProducts = category.product_count > 0;

    Modal.confirm({
      title: t('delete_category_title', { defaultValue: 'Delete Category?' }),
      content: hasProducts
        ? t('delete_category_has_products', {
          count: category.product_count,
          defaultValue: 'This category has {{count}} product(s). The category will be deactivated instead of deleted.',
        })
        : t('delete_category_confirm', { name: category.name, defaultValue: 'Are you sure you want to delete "{{name}}"?' }),
      icon: <WarningOutlined />,
      okText: hasProducts ? t('deactivate', { defaultValue: 'Deactivate' }) : t('delete', { defaultValue: 'Delete' }),
      okType: 'danger',
      onOk: () => {
        deleteCategoryMutation.mutate({
          categoryId: category.id,
          force: hasProducts
        });
      }
    });
  };

  const prepareCategoryData = (values) => {
    const {
      name_ru, description_ru,
      name_en, description_en,
      ...baseValues
    } = values;

    const data = { ...baseValues };

    // Construct translations object
    const translations = {
      name: {},
      description: {}
    };

    if (name_ru) translations.name.ru = name_ru;
    if (name_en) translations.name.en = name_en;

    if (description_ru) translations.description.ru = description_ru;
    if (description_en) translations.description.en = description_en;

    // Only add translations if there are any
    if (Object.keys(translations.name).length > 0 || Object.keys(translations.description).length > 0) {
      data.translations = translations;
    }

    return data;
  };

  const handleCreateSubmit = (values) => {
    createCategoryMutation.mutate(prepareCategoryData(values));
  };

  const handleEditSubmit = (values) => {
    updateCategoryMutation.mutate({
      categoryId: selectedCategory.id,
      categoryData: prepareCategoryData(values)
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

  // Calculate summary statistics
  const categories = data?.data?.items || [];
  const totalCategories = data?.meta?.total || 0;
  const activeCategories = categories.filter(cat => cat.is_active).length;
  const totalProducts = categories.reduce((sum, cat) => sum + (cat.product_count || 0), 0);

  return (
    <div>
      {/* Summary Cards */}
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title={t('total_categories', { defaultValue: 'Total Categories' })}
              value={totalCategories}
              prefix={<TagsOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title={t('active_categories', { defaultValue: 'Active Categories' })}
              value={activeCategories}
              valueStyle={{ color: '#52c41a' }}
              prefix={<TagsOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title={t('total_products', { defaultValue: 'Total Products' })}
              value={totalProducts}
              prefix={<SortAscendingOutlined />}
            />
          </Card>
        </Col>
      </Row>

      <Card>
        {/* Filter Controls */}
        <div className="table-actions">
          <Space wrap>
            <Input.Search
              placeholder={t('search_placeholder', { defaultValue: 'Search categories...' })}
              allowClear
              onSearch={handleSearch}
              style={{ width: 250 }}
              prefix={<SearchOutlined />}
            />
          </Space>

          <Space>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => setIsCreateModalVisible(true)}
            >
              {t('add_category', { defaultValue: 'Add Category' })}
            </Button>
          </Space>
        </div>

        {/* Categories Table */}
        <Table
          columns={columns}
          dataSource={categories}
          loading={isLoading}
          rowKey="id"
          pagination={{
            current: pagination.page,
            pageSize: pagination.per_page,
            total: totalCategories,
            showSizeChanger: true,
            showQuickJumper: true,
            showTotal: (total, range) =>
              t('showing_range', { from: range[0], to: range[1], total, defaultValue: '{{from}}-{{to}} of {{total}} categories' })
          }}
          onChange={handleTableChange}
          className="admin-table"
        />
      </Card>

      {/* Category Details Modal */}
      <Modal
        title={t('category_details_title', { name: selectedCategory?.name, defaultValue: 'Category Details - {{name}}' })}
        open={isDetailModalVisible}
        onCancel={() => setIsDetailModalVisible(false)}
        footer={null}
        width={600}
      >
        {selectedCategory && (
          <div>
            <Row gutter={16}>
              <Col span={24}>
                <h3>{selectedCategory.name}</h3>
                <p><strong>{t('id_label', { defaultValue: 'ID:' })}</strong> {selectedCategory.id}</p>
                <p><strong>{t('sort_order_label', { defaultValue: 'Sort Order:' })}</strong> {selectedCategory.sort_order}</p>
                <p><strong>{t('products_label', { defaultValue: 'Products:' })}</strong> {selectedCategory.product_count}</p>
                <p><strong>{t('status_label', { defaultValue: 'Status:' })}</strong>
                  <Tag color={selectedCategory.is_active ? 'green' : 'red'} style={{ marginLeft: 8 }}>
                    {selectedCategory.is_active ? t('active', { defaultValue: 'Active' }) : t('inactive', { defaultValue: 'Inactive' })}
                  </Tag>
                </p>
              </Col>
            </Row>

            {selectedCategory.description && (
              <>
                <Divider>{t('description', { defaultValue: 'Description' })}</Divider>
                <p>{selectedCategory.description}</p>
              </>
            )}

            {selectedCategory.icon_url && (
              <>
                <Divider>{t('icon', { defaultValue: 'Icon' })}</Divider>
                <img src={selectedCategory.icon_url} alt={selectedCategory.name} style={{ maxWidth: '100px' }} />
              </>
            )}

            <div style={{ marginTop: 16, textAlign: 'right' }}>
              <Space>
                <Button
                  type="primary"
                  onClick={() => {
                    setIsDetailModalVisible(false);
                    handleEditCategory(selectedCategory);
                  }}
                >
                  {t('edit_category', { defaultValue: 'Edit Category' })}
                </Button>
                <Button onClick={() => setIsDetailModalVisible(false)}>
                  {t('close', { defaultValue: 'Close' })}
                </Button>
              </Space>
            </div>
          </div>
        )}
      </Modal>

      {/* Create Category Modal */}
      <Modal
        title={t('add_new_category', { defaultValue: 'Add New Category' })}
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
          <Tabs
            defaultActiveKey="uz"
            items={[
              {
                key: 'uz',
                label: t('tab_uzbek', { defaultValue: 'Uzbek (Default)' }),
                children: (
                  <>
                    <Form.Item
                      name="name"
                      label={t('name_uz', { defaultValue: 'Category Name (Uzbek)' })}
                      rules={[{ required: true, message: t('name_required', { defaultValue: 'Please enter category name' }) }]}
                    >
                      <Input placeholder={t('name_placeholder', { defaultValue: 'Enter category name' })} />
                    </Form.Item>

                    <Form.Item
                      name="description"
                      label={t('description_uz', { defaultValue: 'Description (Uzbek)' })}
                    >
                      <TextArea
                        rows={3}
                        placeholder={t('description_placeholder', { defaultValue: 'Enter category description...' })}
                      />
                    </Form.Item>
                  </>
                )
              },
              {
                key: 'ru',
                label: t('tab_russian', { defaultValue: 'Russian' }),
                children: (
                  <>
                    <Form.Item
                      name="name_ru"
                      label={t('name_ru', { defaultValue: 'Category Name (Russian)' })}
                    >
                      <Input placeholder={t('name_ru_placeholder', { defaultValue: 'Enter Russian category name' })} />
                    </Form.Item>

                    <Form.Item
                      name="description_ru"
                      label={t('description_ru', { defaultValue: 'Description (Russian)' })}
                    >
                      <TextArea
                        rows={3}
                        placeholder={t('description_ru_placeholder', { defaultValue: 'Enter Russian description...' })}
                      />
                    </Form.Item>
                  </>
                )
              },
              {
                key: 'en',
                label: t('tab_english', { defaultValue: 'English' }),
                children: (
                  <>
                    <Form.Item
                      name="name_en"
                      label={t('name_en', { defaultValue: 'Category Name (English)' })}
                    >
                      <Input placeholder={t('name_en_placeholder', { defaultValue: 'Enter English category name' })} />
                    </Form.Item>

                    <Form.Item
                      name="description_en"
                      label={t('description_en', { defaultValue: 'Description (English)' })}
                    >
                      <TextArea
                        rows={3}
                        placeholder={t('description_en_placeholder', { defaultValue: 'Enter English description...' })}
                      />
                    </Form.Item>
                  </>
                )
              }
            ]}
          />

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="sort_order"
                label={t('sort_order', { defaultValue: 'Sort Order' })}
                initialValue={0}
              >
                <InputNumber
                  placeholder="0"
                  style={{ width: '100%' }}
                  min={0}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="is_active"
                label={t('active', { defaultValue: 'Active' })}
                valuePropName="checked"
                initialValue={true}
              >
                <Switch />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item
            name="icon_url"
            label={t('icon_url', { defaultValue: 'Icon URL (optional)' })}
          >
            <Input placeholder={t('icon_url_placeholder', { defaultValue: 'Enter icon URL...' })} />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setIsCreateModalVisible(false)}>
                {t('cancel', { defaultValue: 'Cancel' })}
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={createCategoryMutation.isPending}
              >
                {t('create_category', { defaultValue: 'Create Category' })}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* Edit Category Modal */}
      <Modal
        title={t('edit_category_title', { name: selectedCategory?.name, defaultValue: 'Edit Category - {{name}}' })}
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
          <Tabs
            defaultActiveKey="uz"
            items={[
              {
                key: 'uz',
                label: t('tab_uzbek', { defaultValue: 'Uzbek (Default)' }),
                children: (
                  <>
                    <Form.Item
                      name="name"
                      label={t('name_uz', { defaultValue: 'Category Name (Uzbek)' })}
                      rules={[{ required: true, message: t('name_required', { defaultValue: 'Please enter category name' }) }]}
                    >
                      <Input placeholder={t('name_placeholder', { defaultValue: 'Enter category name' })} />
                    </Form.Item>

                    <Form.Item
                      name="description"
                      label={t('description_uz', { defaultValue: 'Description (Uzbek)' })}
                    >
                      <TextArea
                        rows={3}
                        placeholder={t('description_placeholder', { defaultValue: 'Enter category description...' })}
                      />
                    </Form.Item>
                  </>
                )
              },
              {
                key: 'ru',
                label: t('tab_russian', { defaultValue: 'Russian' }),
                children: (
                  <>
                    <Form.Item
                      name="name_ru"
                      label={t('name_ru', { defaultValue: 'Category Name (Russian)' })}
                    >
                      <Input placeholder={t('name_ru_placeholder', { defaultValue: 'Enter Russian category name' })} />
                    </Form.Item>

                    <Form.Item
                      name="description_ru"
                      label={t('description_ru', { defaultValue: 'Description (Russian)' })}
                    >
                      <TextArea
                        rows={3}
                        placeholder={t('description_ru_placeholder', { defaultValue: 'Enter Russian description...' })}
                      />
                    </Form.Item>
                  </>
                )
              },
              {
                key: 'en',
                label: t('tab_english', { defaultValue: 'English' }),
                children: (
                  <>
                    <Form.Item
                      name="name_en"
                      label={t('name_en', { defaultValue: 'Category Name (English)' })}
                    >
                      <Input placeholder={t('name_en_placeholder', { defaultValue: 'Enter English category name' })} />
                    </Form.Item>

                    <Form.Item
                      name="description_en"
                      label={t('description_en', { defaultValue: 'Description (English)' })}
                    >
                      <TextArea
                        rows={3}
                        placeholder={t('description_en_placeholder', { defaultValue: 'Enter English description...' })}
                      />
                    </Form.Item>
                  </>
                )
              }
            ]}
          />

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="sort_order"
                label={t('sort_order', { defaultValue: 'Sort Order' })}
              >
                <InputNumber
                  placeholder="0"
                  style={{ width: '100%' }}
                  min={0}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="is_active"
                label={t('active', { defaultValue: 'Active' })}
                valuePropName="checked"
              >
                <Switch />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item
            name="icon_url"
            label={t('icon_url', { defaultValue: 'Icon URL (optional)' })}
          >
            <Input placeholder={t('icon_url_placeholder', { defaultValue: 'Enter icon URL...' })} />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setIsEditModalVisible(false)}>
                {t('cancel', { defaultValue: 'Cancel' })}
              </Button>
              <Button
                type="primary"
                htmlType="submit"
                loading={updateCategoryMutation.isPending}
              >
                {t('update_category', { defaultValue: 'Update Category' })}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default ProductCategories;
