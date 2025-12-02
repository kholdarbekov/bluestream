import React, { useState, useRef, useEffect } from 'react';
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
import { useQuery, useMutation, useQueryClient } from 'react-query';
import { Editor } from '@tinymce/tinymce-react';
import adminService from '../services/adminService';
import moment from 'moment';

const { Option } = Select;
const { TextArea } = Input;
const { TabPane } = Tabs;

// TinyMCE API Key from environment variable
const TINYMCE_API_KEY = process.env.REACT_APP_TINYMCE_API_KEY || 'no-api-key';

const Blog = () => {
  const [searchText, setSearchText] = useState('');
  const [categoryFilter, setCategoryFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [selectedPost, setSelectedPost] = useState(null);
  const [isDetailModalVisible, setIsDetailModalVisible] = useState(false);
  const [isCreateModalVisible, setIsCreateModalVisible] = useState(false);
  const [isEditModalVisible, setIsEditModalVisible] = useState(false);
  const [pagination, setPagination] = useState({ page: 1, per_page: 20 });
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();
  const [featuredImageUrl, setFeaturedImageUrl] = useState('');

  const queryClient = useQueryClient();

  // Blog categories
  const categories = [
    { value: 'health_tips', label: 'Health Tips', color: 'green' },
    { value: 'water_benefits', label: 'Water Benefits', color: 'blue' },
    { value: 'company_news', label: 'Company News', color: 'purple' },
    { value: 'quality_assurance', label: 'Quality Assurance', color: 'gold' },
    { value: 'lifestyle', label: 'Lifestyle', color: 'cyan' },
    { value: 'environment', label: 'Environment', color: 'lime' }
  ];

  // Fetch blog posts
  const { data, isLoading } = useQuery(
    ['blog-posts', pagination, searchText, categoryFilter, statusFilter],
    () => adminService.getBlogPosts({
      page: pagination.page,
      per_page: pagination.per_page,
      search: searchText,
      category: categoryFilter,
      status: statusFilter
    }),
    {
      keepPreviousData: true
    }
  );

  // Create post mutation
  const createPostMutation = useMutation(
    (postData) => adminService.createBlogPost(postData),
    {
      onSuccess: () => {
        message.success('Blog post created successfully');
        queryClient.invalidateQueries('blog-posts');
        setIsCreateModalVisible(false);
        createForm.resetFields();
        setFeaturedImageUrl('');
      },
      onError: (error) => {
        message.error(error.response?.data?.error || 'Failed to create blog post');
      }
    }
  );

  // Update post mutation
  const updatePostMutation = useMutation(
    ({ id, data }) => adminService.updateBlogPost(id, data),
    {
      onSuccess: () => {
        message.success('Blog post updated successfully');
        queryClient.invalidateQueries('blog-posts');
        setIsEditModalVisible(false);
        editForm.resetFields();
      },
      onError: (error) => {
        message.error(error.response?.data?.error || 'Failed to update blog post');
      }
    }
  );

  // Delete post mutation
  const deletePostMutation = useMutation(
    (id) => adminService.deleteBlogPost(id),
    {
      onSuccess: () => {
        message.success('Blog post deleted successfully');
        queryClient.invalidateQueries('blog-posts');
      },
      onError: (error) => {
        message.error(error.response?.data?.error || 'Failed to delete blog post');
      }
    }
  );

  // Publish post mutation
  const publishPostMutation = useMutation(
    (id) => adminService.publishBlogPost(id),
    {
      onSuccess: () => {
        message.success('Blog post published successfully');
        queryClient.invalidateQueries('blog-posts');
      },
      onError: (error) => {
        message.error(error.response?.data?.error || 'Failed to publish blog post');
      }
    }
  );

  // Unpublish post mutation
  const unpublishPostMutation = useMutation(
    (id) => adminService.unpublishBlogPost(id),
    {
      onSuccess: () => {
        message.success('Blog post unpublished successfully');
        queryClient.invalidateQueries('blog-posts');
      },
      onError: (error) => {
        message.error(error.response?.data?.error || 'Failed to unpublish blog post');
      }
    }
  );

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
      title: 'Delete Blog Post',
      content: `Are you sure you want to delete "${post.title}"?`,
      okText: 'Yes, Delete',
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
      title: 'Image',
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
      title: 'Title',
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
      title: 'Category',
      dataIndex: 'category',
      key: 'category',
      width: 150,
      render: (category) => {
        const cat = categories.find(c => c.value === category);
        return <Tag color={cat?.color || 'default'}>{cat?.label || category}</Tag>;
      }
    },
    {
      title: 'Status',
      dataIndex: 'status',
      key: 'status',
      width: 120,
      render: (status) => {
        const config = {
          published: { color: 'success', icon: <CheckCircleOutlined />, text: 'Published' },
          draft: { color: 'default', icon: <ClockCircleOutlined />, text: 'Draft' },
          archived: { color: 'warning', icon: <InboxOutlined />, text: 'Archived' }
        };
        const c = config[status] || config.draft;
        return (
          <Tag color={c.color} icon={c.icon}>
            {c.text}
          </Tag>
        );
      }
    },
    {
      title: 'Featured',
      dataIndex: 'is_featured',
      key: 'is_featured',
      width: 100,
      render: (isFeatured) => (
        isFeatured ? <Badge status="success" text="Yes" /> : <Badge status="default" text="No" />
      )
    },
    {
      title: 'Views',
      dataIndex: 'view_count',
      key: 'view_count',
      width: 80,
      render: (count) => count || 0
    },
    {
      title: 'Published',
      dataIndex: 'published_at',
      key: 'published_at',
      width: 120,
      render: (date) => date ? moment(date).format('MMM D, YYYY') : '-'
    },
    {
      title: 'Actions',
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
            Edit
          </Button>
          <Dropdown
            menu={{
              items: [
                {
                  key: 'toggle-publish',
                  label: record.status === 'published' ? 'Unpublish' : 'Publish',
                  icon: record.status === 'published' ? <ClockCircleOutlined /> : <CheckCircleOutlined />,
                  onClick: () => handleTogglePublish(record)
                },
                {
                  key: 'delete',
                  label: 'Delete',
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
        throw new Error('Image upload failed: ' + (error.response?.data?.error || error.message));
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
              label="Title (Uzbek)"
              name="title_uz"
              rules={[{ required: true, message: 'Please enter title in Uzbek' }]}
            >
              <Input placeholder="Enter title in Uzbek" />
            </Form.Item>

            <Form.Item
              label="Excerpt (Uzbek)"
              name="excerpt_uz"
              rules={[{ required: true, message: 'Please enter excerpt in Uzbek' }]}
            >
              <TextArea rows={3} placeholder="Short summary in Uzbek" />
            </Form.Item>

            <Form.Item
              label="Content (Uzbek)"
              rules={[{ required: true, message: 'Please enter content in Uzbek' }]}
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

            <Form.Item label="Author Name (Uzbek)" name="author_name_uz">
              <Input placeholder="Admin" />
            </Form.Item>
          </TabPane>

          <TabPane tab="🇷🇺 Russian" key="ru">
            <Form.Item
              label="Title (Russian)"
              name="title_ru"
              rules={[{ required: true, message: 'Please enter title in Russian' }]}
            >
              <Input placeholder="Enter title in Russian" />
            </Form.Item>

            <Form.Item
              label="Excerpt (Russian)"
              name="excerpt_ru"
              rules={[{ required: true, message: 'Please enter excerpt in Russian' }]}
            >
              <TextArea rows={3} placeholder="Short summary in Russian" />
            </Form.Item>

            <Form.Item
              label="Content (Russian)"
              rules={[{ required: true, message: 'Please enter content in Russian' }]}
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

          <Form.Item label="Author Name (Russian)" name="author_name_ru">
            <Input placeholder="Admin" />
          </Form.Item>
        </TabPane>

        <TabPane tab="🇬🇧 English" key="en">
          <Form.Item
            label="Title (English)"
            name="title_en"
            rules={[{ required: true, message: 'Please enter title in English' }]}
          >
            <Input placeholder="Enter title in English" onChange={(e) => {
              if (!form.getFieldValue('slug')) {
                form.setFieldsValue({ slug: generateSlug(e.target.value) });
              }
            }} />
          </Form.Item>

          <Form.Item
            label="Excerpt (English)"
            name="excerpt_en"
            rules={[{ required: true, message: 'Please enter excerpt in English' }]}
          >
            <TextArea rows={3} placeholder="Short summary in English" />
          </Form.Item>

          <Form.Item
            label="Content (English)"
            rules={[{ required: true, message: 'Please enter content in English' }]}
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

          <Form.Item label="Author Name (English)" name="author_name_en">
            <Input placeholder="Admin" />
          </Form.Item>
        </TabPane>

        <TabPane tab="⚙️ Settings" key="settings">
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                label="Slug"
                name="slug"
                rules={[{ required: true, message: 'Please enter URL slug' }]}
              >
                <Input placeholder="blog-post-slug" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                label="Category"
                name="category"
                rules={[{ required: true, message: 'Please select category' }]}
              >
                <Select placeholder="Select category">
                  {categories.map(cat => (
                    <Option key={cat.value} value={cat.value}>{cat.label}</Option>
                  ))}
                </Select>
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item label="Tags" name="tags">
                <Input placeholder="health, water, tips (comma separated)" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item label="Status" name="status" initialValue="draft">
                <Select>
                  <Option value="draft">Draft</Option>
                  <Option value="published">Published</Option>
                  <Option value="archived">Archived</Option>
                </Select>
              </Form.Item>
            </Col>
          </Row>

          <Divider>Featured Image</Divider>

          <Form.Item label="Upload Image">
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
                  message.error('You can only upload image files!');
                  return Upload.LIST_IGNORE;
                }

                try {
                  // Upload image to server
                  message.loading({ content: 'Uploading image...', key: 'upload' });

                  const response = await adminService.uploadImage(file, {
                    folder: 'blog',
                    resize: true,
                    max_width: 1920,
                    max_height: 1080,
                    quality: 85
                  });

                  message.success({ content: 'Image uploaded successfully!', key: 'upload' });

                  // Use the returned URL
                  const imageUrl = response.data.url;
                  if (onImageChange) {
                    onImageChange(imageUrl);
                  }
                  form.setFieldsValue({ featured_image_url: imageUrl });
                } catch (error) {
                  message.error({
                    content: 'Failed to upload image: ' + (error.response?.data||error.message),
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
                  {localImageUrl ? 'Change Image' : 'Upload Image'}
                </div>
              </div>
            </Upload>
          </Form.Item>

          <Form.Item label="Or Enter Image URL" name="featured_image_url">
            <Input
              placeholder="https://example.com/image.jpg"
              onChange={(e) => {
                if (onImageChange) {
                  onImageChange(e.target.value);
                }
              }}
            />
          </Form.Item>

          <Form.Item label="Image Alt Text" name="image_alt_text">
            <Input placeholder="Description for accessibility" />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item label="Featured on Homepage" name="is_featured" valuePropName="checked" initialValue={false}>
                <Switch />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item label="Sort Order" name="sort_order" initialValue={0}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Divider>SEO Settings</Divider>

          <Form.Item label="Meta Title (Uzbek)" name="meta_title_uz">
            <Input placeholder="SEO title in Uzbek" />
          </Form.Item>

          <Form.Item label="Meta Title (Russian)" name="meta_title_ru">
            <Input placeholder="SEO title in Russian" />
          </Form.Item>

          <Form.Item label="Meta Title (English)" name="meta_title_en">
            <Input placeholder="SEO title in English" />
          </Form.Item>

          <Form.Item label="Meta Description (Uzbek)" name="meta_description_uz">
            <TextArea rows={2} placeholder="SEO description in Uzbek" />
          </Form.Item>

          <Form.Item label="Meta Description (Russian)" name="meta_description_ru">
            <TextArea rows={2} placeholder="SEO description in Russian" />
          </Form.Item>

          <Form.Item label="Meta Description (English)" name="meta_description_en">
            <TextArea rows={2} placeholder="SEO description in English" />
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
            <FileTextOutlined /> Blog Posts
          </h2>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => setIsCreateModalVisible(true)}
          >
            Create Blog Post
          </Button>
        </div>

        <Space style={{ marginBottom: 16, width: '100%', justifyContent: 'space-between' }}>
          <Space>
            <Input
              placeholder="Search posts..."
              prefix={<SearchOutlined />}
              value={searchText}
              onChange={(e) => setSearchText(e.target.value)}
              style={{ width: 250 }}
              allowClear
            />
            <Select
              placeholder="Category"
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
              placeholder="Status"
              style={{ width: 120 }}
              value={statusFilter}
              onChange={setStatusFilter}
              allowClear
            >
              <Option value="draft">Draft</Option>
              <Option value="published">Published</Option>
              <Option value="archived">Archived</Option>
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
            showTotal: (total) => `Total ${total} posts`,
            onChange: (page, pageSize) => setPagination({ page, per_page: pageSize })
          }}
        />
      </Card>

      {/* Create Modal */}
      <Modal
        title="Create Blog Post"
        open={isCreateModalVisible}
        onCancel={() => {
          setIsCreateModalVisible(false);
          createForm.resetFields();
          setFeaturedImageUrl('');
        }}
        footer={[
          <Button key="cancel" onClick={() => setIsCreateModalVisible(false)}>
            Cancel
          </Button>,
          <Button
            key="submit"
            type="primary"
            loading={createPostMutation.isLoading}
            onClick={() => createForm.submit()}
          >
            Create
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
        title="Edit Blog Post"
        open={isEditModalVisible}
        onCancel={() => {
          setIsEditModalVisible(false);
          editForm.resetFields();
          setFeaturedImageUrl('');
        }}
        footer={[
          <Button key="cancel" onClick={() => setIsEditModalVisible(false)}>
            Cancel
          </Button>,
          <Button
            key="submit"
            type="primary"
            loading={updatePostMutation.isLoading}
            onClick={() => editForm.submit()}
          >
            Update
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
