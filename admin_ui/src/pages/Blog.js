import React, { useState, useRef, useEffect } from 'react';
import { DEFAULT_PAGE_SIZE } from '../utils/constants';
import {
  Table,
  Card,
  Input,
  InputNumber,
  Button,
  Space,
  Tag,
  Dropdown,
  Modal,
  Form,
  Select,
  Row,
  Col,
  message,
  Upload,
  Image,
  Switch,
  Divider,
  Tabs,
  DatePicker,
  Badge
} from 'antd';
import {
  SearchOutlined,
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  EyeOutlined,
  MoreOutlined,
  FileTextOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  InboxOutlined,
  ExportOutlined,
  UploadOutlined
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Editor } from '@tinymce/tinymce-react';
import adminService from '../services/adminService';
import { formatDate } from '../utils/dateUtils';

const { Option } = Select;
const { TextArea } = Input;
const { TabPane } = Tabs;

// TinyMCE API Key from environment variable (Vite: import.meta.env.VITE_*)
const TINYMCE_API_KEY = import.meta.env.VITE_TINYMCE_API_KEY || 'no-api-key';

const Blog = () => {
  // Load blog namespace for ui.blog.* keys
  const { t } = useTranslation('blog');
  const [searchText, setSearchText] = useState('');
  const [categoryFilter, setCategoryFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [selectedPost, setSelectedPost] = useState(null);
  const [isDetailModalVisible, setIsDetailModalVisible] = useState(false);
  const [isCreateModalVisible, setIsCreateModalVisible] = useState(false);
  const [isEditModalVisible, setIsEditModalVisible] = useState(false);
  const [pagination, setPagination] = useState({ page: 1, per_page: DEFAULT_PAGE_SIZE });
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();
  const [featuredImageUrl, setFeaturedImageUrl] = useState('');

  const queryClient = useQueryClient();

  // Blog categories
  const categories = [
    { value: 'health_tips', label: t('ui.blog.category_health_tips'), color: 'green' },
    { value: 'water_benefits', label: t('ui.blog.category_water_benefits'), color: 'blue' },
    { value: 'company_news', label: t('ui.blog.category_company_news'), color: 'purple' },
    { value: 'quality_assurance', label: t('ui.blog.category_quality_assurance'), color: 'gold' },
    { value: 'lifestyle', label: t('ui.blog.category_lifestyle'), color: 'cyan' },
    { value: 'environment', label: t('ui.blog.category_environment'), color: 'lime' }
  ];

  // Fetch blog posts
  const { data, isLoading } = useQuery({
    queryKey: ['blog-posts', pagination, searchText, categoryFilter, statusFilter],

    queryFn: () => adminService.getBlogPosts({
      page: pagination.page,
      per_page: pagination.per_page,
      search: searchText,
      category: categoryFilter,
      status: statusFilter
    }),

    placeholderData: keepPreviousData,
  });

  // Create post mutation
  const createPostMutation = useMutation({
    mutationFn: (postData) => adminService.createBlogPost(postData),

    onSuccess: () => {
      message.success(t('ui.blog.created_success'));
      queryClient.invalidateQueries({
        queryKey: ['blog-posts'],
      });
      setIsCreateModalVisible(false);
      createForm.resetFields();
      setFeaturedImageUrl('');
    },

    onError: (error) => {
      message.error(error.response?.data?.error || t('ui.blog.create_failed'));
    },
  });

  // Update post mutation
  const updatePostMutation = useMutation({
    mutationFn: ({ id, data }) => adminService.updateBlogPost(id, data),

    onSuccess: () => {
      message.success(t('ui.blog.updated_success'));
      queryClient.invalidateQueries({
        queryKey: ['blog-posts'],
      });
      setIsEditModalVisible(false);
      editForm.resetFields();
    },

    onError: (error) => {
      message.error(error.response?.data?.error || t('ui.blog.update_failed'));
    },
  });

  // Delete post mutation
  const deletePostMutation = useMutation({
    mutationFn: (id) => adminService.deleteBlogPost(id),

    onSuccess: () => {
      message.success(t('ui.blog.deleted_success'));
      queryClient.invalidateQueries({
        queryKey: ['blog-posts'],
      });
    },

    onError: (error) => {
      message.error(error.response?.data?.error || t('ui.blog.delete_failed'));
    },
  });

  // Publish post mutation
  const publishPostMutation = useMutation({
    mutationFn: (id) => adminService.publishBlogPost(id),

    onSuccess: () => {
      message.success(t('ui.blog.published_success'));
      queryClient.invalidateQueries({
        queryKey: ['blog-posts'],
      });
    },

    onError: (error) => {
      message.error(error.response?.data?.error || t('ui.blog.publish_failed'));
    },
  });

  // Unpublish post mutation
  const unpublishPostMutation = useMutation({
    mutationFn: (id) => adminService.unpublishBlogPost(id),

    onSuccess: () => {
      message.success(t('ui.blog.unpublished_success'));
      queryClient.invalidateQueries({
        queryKey: ['blog-posts'],
      });
    },

    onError: (error) => {
      message.error(error.response?.data?.error || t('ui.blog.unpublish_failed'));
    },
  });

  // Generate slug from title
  const generateSlug = (title) => {
    return title
      .toLowerCase()
      .replace(/[^a-z0-9\s-]/g, '')
      .replace(/\s+/g, '-')
      .replace(/-+/g, '-')
      .trim();
  };

  // Handle create post
  const handleCreatePost = async (values) => {
    const postData = {
      ...values,
      slug: values.slug || generateSlug(values.title_en),
      featured_image: featuredImageUrl
    };
    await createPostMutation.mutateAsync(postData);
  };

  // Handle update post
  const handleUpdatePost = async (values) => {
    const postData = {
      ...values,
      featured_image: featuredImageUrl || selectedPost?.featured_image
    };
    await updatePostMutation.mutateAsync({ id: selectedPost.id, data: postData });
  };

  // Handle delete post
  const handleDeletePost = (post) => {
    Modal.confirm({
      title: t('ui.blog.delete_post'),
      content: `${t('ui.blog.delete_confirm')} "${post.title}"?`,
      okText: t('ui.blog.yes_delete'),
      okType: 'danger',
      onOk: () => deletePostMutation.mutate(post.id)
    });
  };

  // Handle publish/unpublish
  const handleTogglePublish = (post) => {
    if (post.status === 'published') {
      unpublishPostMutation.mutate(post.id);
    } else {
      publishPostMutation.mutate(post.id);
    }
  };

  // Open edit modal
  const openEditModal = (post) => {
    setSelectedPost(post);
    setFeaturedImageUrl(post.featured_image || '');
    editForm.setFieldsValue({
      title_uz: post.title_translations?.uz || post.title,
      title_ru: post.title_translations?.ru || '',
      title_en: post.title_translations?.en || '',
      excerpt_uz: post.excerpt_translations?.uz || post.excerpt,
      excerpt_ru: post.excerpt_translations?.ru || '',
      excerpt_en: post.excerpt_translations?.en || '',
      content_uz: post.content_translations?.uz || post.content,
      content_ru: post.content_translations?.ru || '',
      content_en: post.content_translations?.en || '',
      author_name_uz: post.author_name_translations?.uz || post.author_name,
      author_name_ru: post.author_name_translations?.ru || '',
      author_name_en: post.author_name_translations?.en || '',
      slug: post.slug,
      category: post.category,
      tags: post.tags?.join(', ') || '',
      image_alt_text: post.image_alt_text,
      is_featured: post.is_featured,
      sort_order: post.sort_order,
      status: post.status,
      meta_title_uz: post.meta_title_translations?.uz || '',
      meta_title_ru: post.meta_title_translations?.ru || '',
      meta_title_en: post.meta_title_translations?.en || '',
      meta_description_uz: post.meta_description_translations?.uz || '',
      meta_description_ru: post.meta_description_translations?.ru || '',
      meta_description_en: post.meta_description_translations?.en || ''
    });
    setIsEditModalVisible(true);
  };

  // Table columns
  const columns = [
    {
      title: t('ui.blog.image'),
      dataIndex: 'featured_image',
      key: 'featured_image',
      width: 80,
      render: (image) => (
        image ? (
          <Image src={image} alt="Featured" width={50} height={50} style={{ objectFit: 'cover', borderRadius: 4 }} />
        ) : (
          <div style={{ width: 50, height: 50, background: '#f0f0f0', borderRadius: 4, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <FileTextOutlined style={{ fontSize: 20, color: '#999' }} />
          </div>
        )
      )
    },
    {
      title: t('ui.blog.title'),
      dataIndex: 'title',
      key: 'title',
      ellipsis: true,
      render: (title, record) => (
        <div>
          <div style={{ fontWeight: 500 }}>{title}</div>
          <div style={{ fontSize: 12, color: '#999' }}>/{record.slug}</div>
        </div>
      )
    },
    {
      title: t('ui.blog.category'),
      dataIndex: 'category',
      key: 'category',
      width: 150,
      render: (category) => {
        const cat = categories.find(c => c.value === category);
        return <Tag color={cat?.color || 'default'}>{cat?.label || category}</Tag>;
      }
    },
    {
      title: t('ui.blog.status'),
      dataIndex: 'status',
      key: 'status',
      width: 120,
      render: (status) => {
        const config = {
          published: { color: 'success', icon: <CheckCircleOutlined />, text: t('ui.blog.status_published') },
          draft: { color: 'default', icon: <ClockCircleOutlined />, text: t('ui.blog.status_draft') },
          archived: { color: 'warning', icon: <InboxOutlined />, text: t('ui.blog.status_archived') }
        };
        // eslint-disable-next-line security/detect-object-injection
        const c = config[status] || config.draft;
        return (
          <Tag color={c.color} icon={c.icon}>
            {c.text}
          </Tag>
        );
      }
    },
    {
      title: t('ui.blog.featured'),
      dataIndex: 'is_featured',
      key: 'is_featured',
      width: 100,
      render: (isFeatured) => (
        isFeatured ? <Badge status="success" text={t('ui.blog.yes')} /> : <Badge status="default" text={t('ui.blog.no')} />
      )
    },
    {
      title: t('ui.blog.views'),
      dataIndex: 'view_count',
      key: 'view_count',
      width: 80,
      render: (count) => count || 0
    },
    {
      title: t('ui.blog.published'),
      dataIndex: 'published_at',
      key: 'published_at',
      width: 120,
      render: (date) => date ? formatDate(date, 'MMM D, YYYY') : '-'
    },
    {
      title: t('ui.blog.actions'),
      key: 'actions',
      width: 100,
      render: (_, record) => (
        <Space>
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={() => openEditModal(record)}
          >
            {t('ui.blog.edit')}
          </Button>
          <Dropdown
            menu={{
              items: [
                {
                  key: 'toggle-publish',
                  label: record.status === 'published' ? t('ui.blog.unpublish') : t('ui.blog.publish'),
                  icon: record.status === 'published' ? <ClockCircleOutlined /> : <CheckCircleOutlined />,
                  onClick: () => handleTogglePublish(record)
                },
                {
                  key: 'delete',
                  label: t('ui.blog.delete'),
                  icon: <DeleteOutlined />,
                  danger: true,
                  onClick: () => handleDeletePost(record)
                }
              ]
            }}
            trigger={['click']}
          >
            <Button type="text" size="small" icon={<MoreOutlined />} />
          </Dropdown>
        </Space>
      )
    }
  ];

  // TinyMCE configuration
  const editorConfig = {
    height: 400,
    menubar: true,

    // Disable premium features and telemetry
    promotion: false,
    branding: false,

    // Only use free/open-source plugins
    plugins: [
      'advlist', 'autolink', 'lists', 'link', 'image', 'charmap', 'preview',
      'anchor', 'searchreplace', 'visualblocks', 'code', 'fullscreen',
      'insertdatetime', 'media', 'table', 'help', 'wordcount',
      'codesample', 'emoticons'
    ],
    toolbar: 'undo redo | blocks fontfamily fontsize | bold italic underline strikethrough forecolor backcolor | alignleft aligncenter alignright alignjustify | bullist numlist outdent indent | removeformat | image media link table | codesample emoticons charmap | code fullscreen preview | help',
    content_style: 'body { font-family:Helvetica,Arial,sans-serif; font-size:14px }',

    // Image handling - upload to server
    images_upload_handler: async (blobInfo) => {
      try {
        // Create file from blob
        const file = blobInfo.blob();

        // Upload image to server
        const response = await adminService.uploadImage(file, {
          folder: 'blog-content',
          resize: true,
          max_width: 1920,
          max_height: 1080,
          quality: 85
        });

        // Return the URL from server
        return response.data.url;
      } catch (error) {
        throw new Error(`Image upload failed: ${  error.response?.data?.error || error.message}`);
      }
    },
    automatic_uploads: true,
    file_picker_types: 'image',
    paste_data_images: true,
    image_advtab: true,
    image_caption: true,
    image_title: true,
    image_uploadtab: true,

    // Enable image resizing
    object_resizing: true,
    resize_img_proportional: true,

    // Table features
    table_use_colgroups: true,
    table_sizing_mode: 'responsive',

    // Spell checker
    browser_spellcheck: true
  };

  // Blog post form (reusable for create and edit)
  const BlogPostForm = ({ form, onFinish, imageUrl, onImageChange }) => {
    const localImageUrl = imageUrl || form.getFieldValue('featured_image_url') || '';

    return (
      <Form form={form} layout="vertical" onFinish={onFinish}>
        <Tabs defaultActiveKey="uz">
          <TabPane tab="🇺🇿 Uzbek" key="uz">
            <Form.Item
              label={t('ui.blog.form_title_uz')}
              name="title_uz"
              rules={[{ required: true, message: t('ui.blog.form_title_uz_required') }]}
            >
              <Input placeholder={t('ui.blog.form_title_uz_placeholder')} />
            </Form.Item>

            <Form.Item
              label={t('ui.blog.form_excerpt_uz')}
              name="excerpt_uz"
              rules={[{ required: true, message: t('ui.blog.form_excerpt_uz_required') }]}
            >
              <TextArea rows={3} placeholder={t('ui.blog.form_excerpt_uz_placeholder')} />
            </Form.Item>

            <Form.Item
              label={t('ui.blog.form_content_uz')}
              rules={[{ required: true, message: t('ui.blog.form_content_uz_required') }]}
            >
              <Editor
                apiKey={TINYMCE_API_KEY}
                init={editorConfig}
                initialValue={form.getFieldValue('content_uz') || ''}
                onEditorChange={(content) => {
                  form.setFieldsValue({ content_uz: content });
                }}
              />
            </Form.Item>
            {/* Hidden field to store actual content value */}
            <Form.Item name="content_uz" hidden>
              <Input type="hidden" />
            </Form.Item>

            <Form.Item label={t('ui.blog.form_author_uz')} name="author_name_uz">
              <Input placeholder={t('ui.blog.form_author_placeholder')} />
            </Form.Item>
          </TabPane>

          <TabPane tab="🇷🇺 Russian" key="ru">
            <Form.Item
              label={t('ui.blog.form_title_ru')}
              name="title_ru"
              rules={[{ required: true, message: t('ui.blog.form_title_ru_required') }]}
            >
              <Input placeholder={t('ui.blog.form_title_ru_placeholder')} />
            </Form.Item>

            <Form.Item
              label={t('ui.blog.form_excerpt_ru')}
              name="excerpt_ru"
              rules={[{ required: true, message: t('ui.blog.form_excerpt_ru_required') }]}
            >
              <TextArea rows={3} placeholder={t('ui.blog.form_excerpt_ru_placeholder')} />
            </Form.Item>

            <Form.Item
              label={t('ui.blog.form_content_ru')}
              rules={[{ required: true, message: t('ui.blog.form_content_ru_required') }]}
            >
              <Editor
                apiKey={TINYMCE_API_KEY}
                init={editorConfig}
                initialValue={form.getFieldValue('content_ru') || ''}
                onEditorChange={(content) => {
                  form.setFieldsValue({ content_ru: content });
                }}
              />
          </Form.Item>
          {/* Hidden field to store actual content value */}
          <Form.Item name="content_ru" hidden>
            <Input type="hidden" />
          </Form.Item>

          <Form.Item label={t('ui.blog.form_author_ru')} name="author_name_ru">
            <Input placeholder={t('ui.blog.form_author_placeholder')} />
          </Form.Item>
        </TabPane>

        <TabPane tab="🇬🇧 English" key="en">
          <Form.Item
            label={t('ui.blog.form_title_en')}
            name="title_en"
            rules={[{ required: true, message: t('ui.blog.form_title_en_required') }]}
          >
            <Input placeholder={t('ui.blog.form_title_en_placeholder')} onChange={(e) => {
              if (!form.getFieldValue('slug')) {
                form.setFieldsValue({ slug: generateSlug(e.target.value) });
              }
            }} />
          </Form.Item>

          <Form.Item
            label={t('ui.blog.form_excerpt_en')}
            name="excerpt_en"
            rules={[{ required: true, message: t('ui.blog.form_excerpt_en_required') }]}
          >
            <TextArea rows={3} placeholder={t('ui.blog.form_excerpt_en_placeholder')} />
          </Form.Item>

          <Form.Item
            label={t('ui.blog.form_content_en')}
            rules={[{ required: true, message: t('ui.blog.form_content_en_required') }]}
          >
            <Editor
              apiKey={TINYMCE_API_KEY}
              init={editorConfig}
              initialValue={form.getFieldValue('content_en') || ''}
              onEditorChange={(content) => {
                form.setFieldsValue({ content_en: content });
              }}
            />
          </Form.Item>
          {/* Hidden field to store actual content value */}
          <Form.Item name="content_en" hidden>
            <Input type="hidden" />
          </Form.Item>

          <Form.Item label={t('ui.blog.form_author_en')} name="author_name_en">
            <Input placeholder={t('ui.blog.form_author_placeholder')} />
          </Form.Item>
        </TabPane>

        <TabPane tab={`⚙️ ${t('ui.blog.form_settings')}`} key="settings">
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                label={t('ui.blog.form_slug')}
                name="slug"
                rules={[{ required: true, message: t('ui.blog.form_slug_required') }]}
              >
                <Input placeholder={t('ui.blog.form_slug_placeholder')} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                label={t('ui.blog.form_category')}
                name="category"
                rules={[{ required: true, message: t('ui.blog.form_category_required') }]}
              >
                <Select placeholder={t('ui.blog.form_category_placeholder')}>
                  {categories.map(cat => (
                    <Option key={cat.value} value={cat.value}>{cat.label}</Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item label={t('ui.blog.form_tags')} name="tags">
                <Input placeholder={t('ui.blog.form_tags_placeholder')} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item label={t('ui.blog.form_status')} name="status" initialValue="draft">
                <Select>
                  <Option value="draft">{t('ui.blog.status_draft')}</Option>
                  <Option value="published">{t('ui.blog.status_published')}</Option>
                  <Option value="archived">{t('ui.blog.status_archived')}</Option>
                </Select>
              </Form.Item>
            </Col>
          </Row>

          <Divider>{t('ui.blog.form_featured_image')}</Divider>

          <Form.Item label={t('ui.blog.form_upload_image')}>
            <Upload
              name="file"
              listType="picture-card"
              fileList={localImageUrl ? [
                {
                  uid: '-1',
                  name: 'featured-image',
                  status: 'done',
                  url: localImageUrl,
                }
              ] : []}
              beforeUpload={async (file) => {
                const isImage = file.type.startsWith('image/');
                if (!isImage) {
                  message.error(t('ui.blog.form_upload_error_type'));
                  return Upload.LIST_IGNORE;
                }

                try {
                  // Upload image to server
                  message.loading({ content: t('ui.blog.form_uploading'), key: 'upload' });

                  const response = await adminService.uploadImage(file, {
                    folder: 'blog',
                    resize: true,
                    max_width: 1920,
                    max_height: 1080,
                    quality: 85
                  });

                  message.success({ content: t('ui.blog.form_upload_success'), key: 'upload' });

                  // Use the returned URL
                  const imageUrl = response.data.url;
                  if (onImageChange) {
                    onImageChange(imageUrl);
                  }
                  form.setFieldsValue({ featured_image_url: imageUrl });
                } catch (error) {
                  message.error({
                    content: `${t('ui.blog.form_upload_failed')  }: ${  error.response?.data||error.message}`,
                    key: 'upload'
                  });
                }

                return false; // Prevent auto upload
              }}
              onRemove={() => {
                if (onImageChange) {
                  onImageChange('');
                }
                form.setFieldsValue({ featured_image_url: '' });
              }}
            >
              <div>
                <PlusOutlined />
                <div style={{ marginTop: 8 }}>
                  {localImageUrl ? t('ui.blog.form_change_image') : t('ui.blog.form_upload_image')}
                </div>
              </div>
            </Upload>
          </Form.Item>

          <Form.Item label={t('ui.blog.form_or_enter_url')} name="featured_image_url">
            <Input
              placeholder={t('ui.blog.form_image_url_placeholder')}
              onChange={(e) => {
                if (onImageChange) {
                  onImageChange(e.target.value);
                }
              }}
            />
          </Form.Item>

          <Form.Item label={t('ui.blog.form_image_alt')} name="image_alt_text">
            <Input placeholder={t('ui.blog.form_image_alt_placeholder')} />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item label={t('ui.blog.form_featured_homepage')} name="is_featured" valuePropName="checked" initialValue={false}>
                <Switch />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item label={t('ui.blog.form_sort_order')} name="sort_order" initialValue={0}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Divider>{t('ui.blog.form_seo_settings')}</Divider>

          <Form.Item label={t('ui.blog.form_meta_title_uz')} name="meta_title_uz">
            <Input placeholder={t('ui.blog.form_meta_title_uz_placeholder')} />
          </Form.Item>

          <Form.Item label={t('ui.blog.form_meta_title_ru')} name="meta_title_ru">
            <Input placeholder={t('ui.blog.form_meta_title_ru_placeholder')} />
          </Form.Item>

          <Form.Item label={t('ui.blog.form_meta_title_en')} name="meta_title_en">
            <Input placeholder={t('ui.blog.form_meta_title_en_placeholder')} />
          </Form.Item>

          <Form.Item label={t('ui.blog.form_meta_description_uz')} name="meta_description_uz">
            <TextArea rows={2} placeholder={t('ui.blog.form_meta_description_uz_placeholder')} />
          </Form.Item>

          <Form.Item label={t('ui.blog.form_meta_description_ru')} name="meta_description_ru">
            <TextArea rows={2} placeholder={t('ui.blog.form_meta_description_ru_placeholder')} />
          </Form.Item>

          <Form.Item label={t('ui.blog.form_meta_description_en')} name="meta_description_en">
            <TextArea rows={2} placeholder={t('ui.blog.form_meta_description_en_placeholder')} />
          </Form.Item>
        </TabPane>
      </Tabs>
    </Form>
    );
  };

  return (
    <div style={{ padding: 24 }}>
      <Card>
        <div style={{ marginBottom: 16, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h2 style={{ margin: 0 }}>
            <FileTextOutlined /> {t('ui.blog.blog_posts')}
          </h2>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => setIsCreateModalVisible(true)}
          >
            {t('ui.blog.create_blog_post')}
          </Button>
        </div>

        <Space style={{ marginBottom: 16, width: '100%', justifyContent: 'space-between' }}>
          <Space>
            <Input
              placeholder={t('ui.blog.search_posts')}
              prefix={<SearchOutlined />}
              value={searchText}
              onChange={(e) => setSearchText(e.target.value)}
              style={{ width: 250 }}
              allowClear
            />
            <Select
              placeholder={t('ui.blog.category')}
              style={{ width: 150 }}
              value={categoryFilter}
              onChange={setCategoryFilter}
              allowClear
            >
              {categories.map(cat => (
                <Option key={cat.value} value={cat.value}>{cat.label}</Option>
              ))}
            </Select>
            <Select
              placeholder={t('ui.blog.status')}
              style={{ width: 120 }}
              value={statusFilter}
              onChange={setStatusFilter}
              allowClear
            >
              <Option value="draft">{t('ui.blog.status_draft')}</Option>
              <Option value="published">{t('ui.blog.status_published')}</Option>
              <Option value="archived">{t('ui.blog.status_archived')}</Option>
            </Select>
          </Space>
        </Space>

        <Table
          columns={columns}
          dataSource={data?.data?.items || []}
          loading={isLoading}
          rowKey="id"
          pagination={{
            current: pagination.page,
            pageSize: pagination.per_page,
            total: data?.meta?.total || 0,
            showSizeChanger: true,
            showTotal: (total) => `${t('ui.blog.total')} ${total} ${t('ui.blog.posts')}`,
            onChange: (page, pageSize) => setPagination({ page, per_page: pageSize })
          }}
        />
      </Card>

      {/* Create Modal */}
      <Modal
        title={t('ui.blog.create_blog_post')}
        open={isCreateModalVisible}
        onCancel={() => {
          setIsCreateModalVisible(false);
          createForm.resetFields();
          setFeaturedImageUrl('');
        }}
        footer={[
          <Button key="cancel" onClick={() => setIsCreateModalVisible(false)}>
            {t('ui.blog.cancel')}
          </Button>,
          <Button
            key="submit"
            type="primary"
            loading={createPostMutation.isPending}
            onClick={() => createForm.submit()}
          >
            {t('ui.blog.create')}
          </Button>
        ]}
        width={900}
      >
        <BlogPostForm
          form={createForm}
          onFinish={handleCreatePost}
          imageUrl={featuredImageUrl}
          onImageChange={setFeaturedImageUrl}
        />
      </Modal>

      {/* Edit Modal */}
      <Modal
        title={t('ui.blog.edit_blog_post')}
        open={isEditModalVisible}
        onCancel={() => {
          setIsEditModalVisible(false);
          editForm.resetFields();
          setFeaturedImageUrl('');
        }}
        footer={[
          <Button key="cancel" onClick={() => setIsEditModalVisible(false)}>
            {t('ui.blog.cancel')}
          </Button>,
          <Button
            key="submit"
            type="primary"
            loading={updatePostMutation.isPending}
            onClick={() => editForm.submit()}
          >
            {t('ui.blog.update')}
          </Button>
        ]}
        width={900}
      >
        <BlogPostForm
          form={editForm}
          onFinish={handleUpdatePost}
          imageUrl={featuredImageUrl}
          onImageChange={setFeaturedImageUrl}
        />
      </Modal>
    </div>
  );
};

export default Blog;
