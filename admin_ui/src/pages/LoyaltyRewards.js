import React, { useMemo, useState } from 'react';
import { DEFAULT_PAGE_SIZE } from '../utils/constants';
import {
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Drawer,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tag,
  Typography,
  message
} from 'antd';
import {
  DeleteOutlined,
  EditOutlined,
  ExportOutlined,
  EyeOutlined,
  GiftOutlined,
  PlusOutlined,
  StarOutlined
} from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';
import adminService from '../services/adminService';
import exportUtils from '../utils/exportUtils';
import { formatDate, formatDateTime } from '../utils/dateUtils';
import AsyncButton from '../components/common/AsyncButton';
import DataView from '../components/common/DataView';
import EmptyState from '../components/common/EmptyState';

const { TextArea } = Input;
const rewardTypeOptions = [
  { value: 'discount', label: 'Discount' },
  { value: 'free_product', label: 'Free Product' },
  { value: 'free_delivery', label: 'Free Delivery' },
  { value: 'voucher', label: 'Voucher' },
];

const LoyaltyRewards = () => {
  const { t } = useTranslation('loyalty');
  const queryClient = useQueryClient();
  const [searchText, setSearchText] = useState('');
  const [programId, setProgramId] = useState();
  const [rewardType, setRewardType] = useState();
  const [statusFilter, setStatusFilter] = useState();
  const [pagination, setPagination] = useState({ page: 1, per_page: DEFAULT_PAGE_SIZE });
  const [drawerRewardId, setDrawerRewardId] = useState(null);
  const [rewardModal, setRewardModal] = useState({ open: false, reward: null });
  const [rewardForm] = Form.useForm();

  const rewardsQuery = useQuery({
    queryKey: ['loyalty-rewards', pagination, searchText, programId, rewardType, statusFilter],

    queryFn: () => adminService.getLoyaltyRewards({
      page: pagination.page,
      per_page: pagination.per_page,
      search: searchText,
      program_id: programId,
      reward_type: rewardType,
      is_active: statusFilter,
    }),

    placeholderData: keepPreviousData,
  });

  const programsQuery = useQuery({
    queryKey: ['loyalty-program-options'],
    queryFn: () => adminService.getLoyaltyPrograms({ page: 1, per_page: 100 }),
    placeholderData: keepPreviousData,
  });

  const rewardDetailQuery = useQuery({
    queryKey: ['loyalty-reward-detail', drawerRewardId],
    queryFn: () => adminService.getLoyaltyReward(drawerRewardId),
    enabled: Boolean(drawerRewardId),
  });

  const invalidateRewardQueries = () => {
    queryClient.invalidateQueries({
      queryKey: ['loyalty-rewards'],
    });
    queryClient.invalidateQueries({
      queryKey: ['analytics-loyalty'],
    });
  };

  const createRewardMutation = useMutation({
    mutationFn: (values) => adminService.createLoyaltyReward(values),

    onSuccess: () => {
      message.success(t('ui.loyalty.reward_create_success', { defaultValue: 'Reward created successfully' }));
      setRewardModal({ open: false, reward: null });
      rewardForm.resetFields();
      invalidateRewardQueries();
    },
  });

  const updateRewardMutation = useMutation({
    mutationFn: ({ rewardId, values }) => adminService.updateLoyaltyReward(rewardId, values),

    onSuccess: () => {
      message.success(t('ui.loyalty.reward_update_success', { defaultValue: 'Reward updated successfully' }));
      setRewardModal({ open: false, reward: null });
      rewardForm.resetFields();
      invalidateRewardQueries();
    },
  });

  const deleteRewardMutation = useMutation({
    mutationFn: (rewardId) => adminService.deleteLoyaltyReward(rewardId),

    onSuccess: () => {
      message.success(t('ui.loyalty.reward_delete_success', { defaultValue: 'Reward removed successfully' }));
      invalidateRewardQueries();
    },
  });

  const rewards = rewardsQuery.data?.items || [];
  const totalRewards = rewardsQuery.data?.total || 0;
  const programs = programsQuery.data?.items || [];
  const rewardTypeValue = Form.useWatch('reward_type', rewardForm);

  const columns = useMemo(() => ([
    {
      title: t('ui.loyalty.reward_name', { defaultValue: 'Reward Name' }),
      dataIndex: 'name',
      key: 'name',
      render: (_, record) => (
        <div>
          <div style={{ fontWeight: 600 }}>{record.name}</div>
          <Typography.Text type="secondary">{record.program_name || '-'}</Typography.Text>
        </div>
      )
    },
    {
      title: t('ui.loyalty.type', { defaultValue: 'Type' }),
      dataIndex: 'reward_type',
      key: 'reward_type',
      width: 140,
      render: (value) => <Tag color="blue">{value}</Tag>
    },
    {
      title: t('ui.loyalty.points_cost', { defaultValue: 'Points Cost' }),
      dataIndex: 'points_cost',
      key: 'points_cost',
      width: 140,
    },
    {
      title: t('ui.loyalty.redemptions', { defaultValue: 'Redemptions' }),
      dataIndex: 'redemptions_used',
      key: 'redemptions_used',
      width: 140,
    },
    {
      title: t('ui.loyalty.status', { defaultValue: 'Status' }),
      dataIndex: 'is_active',
      key: 'is_active',
      width: 120,
      render: (value) => <Tag color={value ? 'green' : 'red'}>{value ? 'Active' : 'Inactive'}</Tag>
    },
    {
      title: t('ui.loyalty.actions', { defaultValue: 'Actions' }),
      key: 'actions',
      width: 140,
      render: (_, record) => (
        <Space>
          <Button type="text" icon={<EyeOutlined />} onClick={() => setDrawerRewardId(record.id)} />
          <Button
            type="text"
            icon={<EditOutlined />}
            onClick={() => {
              setRewardModal({ open: true, reward: record });
              rewardForm.setFieldsValue({
                ...record,
                valid_from: record.valid_from ? dayjs(record.valid_from) : null,
                valid_until: record.valid_until ? dayjs(record.valid_until) : null,
              });
            }}
          />
          <Button
            type="text"
            danger
            icon={<DeleteOutlined />}
            onClick={() => {
              Modal.confirm({
                title: t('ui.loyalty.reward_delete_confirm_title', { defaultValue: 'Delete reward?' }),
                content: t('ui.loyalty.reward_delete_confirm_message', { defaultValue: `Delete ${record.name}?` }),
                onOk: () => deleteRewardMutation.mutate(record.id),
              });
            }}
          />
        </Space>
      )
    }
  ]), [deleteRewardMutation, rewardForm, t]);

  const handleExport = async () => {
    const result = await exportUtils.exportLoyaltyRewards({
      search: searchText,
      program_id: programId,
      reward_type: rewardType,
      is_active: statusFilter,
    });
    if (!result.success) {
      message.error(result.message);
    }
  };

  return (
    <div>
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} md={8}>
          <Card>
            <Statistic
              title={t('ui.loyalty.total_rewards', { defaultValue: 'Total Rewards' })}
              value={totalRewards}
              prefix={<GiftOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} md={8}>
          <Card>
            <Statistic
              title={t('ui.loyalty.active_rewards', { defaultValue: 'Active Rewards' })}
              value={rewards.filter((reward) => reward.is_active).length}
              prefix={<StarOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} md={8}>
          <Card>
            <Statistic
              title={t('ui.loyalty.featured_rewards', { defaultValue: 'Featured Rewards' })}
              value={rewards.filter((reward) => reward.is_featured).length}
              prefix={<StarOutlined />}
            />
          </Card>
        </Col>
      </Row>

      <Card>
        <div className="table-actions">
          <Space wrap>
            <Input.Search
              allowClear
              placeholder={t('ui.loyalty.search_rewards', { defaultValue: 'Search rewards' })}
              style={{ width: 240 }}
              value={searchText}
              onChange={(event) => {
                setSearchText(event.target.value);
                setPagination((current) => ({ ...current, page: 1 }));
              }}
            />
            <Select
              allowClear
              placeholder={t('ui.loyalty.program', { defaultValue: 'Program' })}
              style={{ width: 200 }}
              value={programId}
              onChange={(value) => {
                setProgramId(value);
                setPagination((current) => ({ ...current, page: 1 }));
              }}
              options={programs.map((program) => ({
                value: program.id,
                label: program.name,
              }))}
            />
            <Select
              allowClear
              placeholder={t('ui.loyalty.type', { defaultValue: 'Type' })}
              style={{ width: 180 }}
              value={rewardType}
              onChange={(value) => {
                setRewardType(value);
                setPagination((current) => ({ ...current, page: 1 }));
              }}
              options={rewardTypeOptions}
            />
            <Select
              allowClear
              placeholder={t('ui.loyalty.status', { defaultValue: 'Status' })}
              style={{ width: 160 }}
              value={statusFilter}
              onChange={(value) => {
                setStatusFilter(value);
                setPagination((current) => ({ ...current, page: 1 }));
              }}
              options={[
                { value: 'true', label: 'Active' },
                { value: 'false', label: 'Inactive' },
              ]}
            />
          </Space>

          <Space>
            <Button icon={<ExportOutlined />} onClick={handleExport}>
              {t('ui.loyalty.export_rewards', { defaultValue: 'Export Rewards' })}
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => {
                setRewardModal({ open: true, reward: null });
                rewardForm.resetFields();
                rewardForm.setFieldsValue({
                  is_active: true,
                  is_featured: false,
                  reward_type: 'voucher',
                  max_uses_per_user: 1,
                  sort_order: 0,
                });
              }}
            >
              {t('ui.loyalty.create_reward', { defaultValue: 'Create Reward' })}
            </Button>
          </Space>
        </div>

        <Table
          rowKey="id"
          columns={columns}
          dataSource={rewards}
          loading={rewardsQuery.isLoading}
          locale={{
            emptyText: <EmptyState description={t('ui.loyalty.no_rewards', { defaultValue: 'No loyalty rewards found' })} />
          }}
          pagination={{
            current: pagination.page,
            pageSize: pagination.per_page,
            total: totalRewards,
            showSizeChanger: true,
          }}
          onChange={(pageInfo) => {
            setPagination({
              page: pageInfo.current,
              per_page: pageInfo.pageSize,
            });
          }}
        />
      </Card>

      <Drawer
        open={Boolean(drawerRewardId)}
        width={720}
        title={rewardDetailQuery.data?.name || t('ui.loyalty.reward_details', { defaultValue: 'Reward Details' })}
        onClose={() => setDrawerRewardId(null)}
      >
        <DataView
          loading={rewardDetailQuery.isLoading}
          error={rewardDetailQuery.error}
          isEmpty={!rewardDetailQuery.data}
          onRetry={() => rewardDetailQuery.refetch()}
          emptyDescription={t('ui.loyalty.no_reward_details', { defaultValue: 'No reward selected' })}
        >
          <Descriptions bordered column={2} size="small">
            <Descriptions.Item label={t('ui.loyalty.program', { defaultValue: 'Program' })}>
              {rewardDetailQuery.data.program_name || '-'}
            </Descriptions.Item>
            <Descriptions.Item label={t('ui.loyalty.type', { defaultValue: 'Type' })}>
              {rewardDetailQuery.data.reward_type || '-'}
            </Descriptions.Item>
            <Descriptions.Item label={t('ui.loyalty.points_cost', { defaultValue: 'Points Cost' })}>
              {rewardDetailQuery.data.points_cost || 0}
            </Descriptions.Item>
            <Descriptions.Item label={t('ui.loyalty.redemptions', { defaultValue: 'Redemptions' })}>
              {rewardDetailQuery.data.redemptions_used || 0}
            </Descriptions.Item>
            <Descriptions.Item label={t('ui.loyalty.valid_from', { defaultValue: 'Valid From' })}>
              {rewardDetailQuery.data.valid_from ? formatDateTime(rewardDetailQuery.data.valid_from) : '-'}
            </Descriptions.Item>
            <Descriptions.Item label={t('ui.loyalty.valid_until', { defaultValue: 'Valid Until' })}>
              {rewardDetailQuery.data.valid_until ? formatDateTime(rewardDetailQuery.data.valid_until) : '-'}
            </Descriptions.Item>
            <Descriptions.Item label={t('ui.loyalty.featured', { defaultValue: 'Featured' })}>
              {rewardDetailQuery.data.is_featured ? 'Yes' : 'No'}
            </Descriptions.Item>
            <Descriptions.Item label={t('ui.loyalty.status', { defaultValue: 'Status' })}>
              {rewardDetailQuery.data.is_active ? 'Active' : 'Inactive'}
            </Descriptions.Item>
            <Descriptions.Item label={t('ui.loyalty.description', { defaultValue: 'Description' })} span={2}>
              {rewardDetailQuery.data.description || '-'}
            </Descriptions.Item>
            <Descriptions.Item label={t('ui.loyalty.terms', { defaultValue: 'Terms' })} span={2}>
              {rewardDetailQuery.data?.terms_conditions || '-'}
            </Descriptions.Item>
          </Descriptions>
        </DataView>
      </Drawer>

      <Modal
        open={rewardModal.open}
        title={rewardModal.reward ? t('ui.loyalty.edit_reward', { defaultValue: 'Edit Reward' }) : t('ui.loyalty.create_reward', { defaultValue: 'Create Reward' })}
        onCancel={() => setRewardModal({ open: false, reward: null })}
        footer={null}
        width={820}
      >
        <Form
          form={rewardForm}
          layout="vertical"
          onFinish={(values) => {
            const payload = {
              ...values,
              valid_from: values.valid_from ? values.valid_from.toISOString() : null,
              valid_until: values.valid_until ? values.valid_until.toISOString() : null,
            };

            if (rewardModal.reward) {
              updateRewardMutation.mutate({ rewardId: rewardModal.reward.id, values: payload });
              return;
            }
            createRewardMutation.mutate(payload);
          }}
        >
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="program_id" label={t('ui.loyalty.program', { defaultValue: 'Program' })} rules={[{ required: true }]}>
                <Select options={programs.map((program) => ({ value: program.id, label: program.name }))} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="reward_type" label={t('ui.loyalty.type', { defaultValue: 'Type' })} rules={[{ required: true }]}>
                <Select options={rewardTypeOptions} />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="name" label={t('ui.loyalty.reward_name', { defaultValue: 'Reward Name' })} rules={[{ required: true }]}>
                <Input />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="points_cost" label={t('ui.loyalty.points_cost', { defaultValue: 'Points Cost' })} rules={[{ required: true }]}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item name="description" label={t('ui.loyalty.description', { defaultValue: 'Description' })}>
            <TextArea rows={3} />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="min_order_value" label={t('ui.loyalty.min_order_value', { defaultValue: 'Minimum Order Value' })}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="max_uses_per_user" label={t('ui.loyalty.max_uses_per_user', { defaultValue: 'Max Uses per User' })}>
                <InputNumber min={1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="max_redemptions" label={t('ui.loyalty.max_redemptions', { defaultValue: 'Max Redemptions' })}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="sort_order" label={t('ui.loyalty.sort_order', { defaultValue: 'Sort Order' })}>
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          {rewardTypeValue === 'discount' ? (
            <Row gutter={16}>
              <Col span={12}>
                <Form.Item name="discount_type" label={t('ui.loyalty.discount_type', { defaultValue: 'Discount Type' })}>
                  <Select options={[{ value: 'percentage', label: 'Percentage' }, { value: 'fixed', label: 'Fixed' }]} />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item name="discount_value" label={t('ui.loyalty.discount_value', { defaultValue: 'Discount Value' })}>
                  <InputNumber min={0} style={{ width: '100%' }} />
                </Form.Item>
              </Col>
            </Row>
          ) : null}

          {rewardTypeValue === 'free_product' ? (
            <Form.Item name="free_product_id" label={t('ui.loyalty.free_product_id', { defaultValue: 'Free Product ID' })}>
              <InputNumber min={1} style={{ width: '100%' }} />
            </Form.Item>
          ) : null}

          {rewardTypeValue === 'voucher' ? (
            <Form.Item name="voucher_code" label={t('ui.loyalty.voucher_code', { defaultValue: 'Voucher Code' })}>
              <Input />
            </Form.Item>
          ) : null}

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="valid_from" label={t('ui.loyalty.valid_from', { defaultValue: 'Valid From' })}>
                <DatePicker showTime style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="valid_until" label={t('ui.loyalty.valid_until', { defaultValue: 'Valid Until' })}>
                <DatePicker showTime style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item name="image_url" label={t('ui.loyalty.image_url', { defaultValue: 'Image URL' })}>
            <Input />
          </Form.Item>
          <Form.Item name="terms_conditions" label={t('ui.loyalty.terms', { defaultValue: 'Terms and Conditions' })}>
            <TextArea rows={2} />
          </Form.Item>

          <Space size={24}>
            <Form.Item name="is_active" label={t('ui.loyalty.active', { defaultValue: 'Active' })} valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="is_featured" label={t('ui.loyalty.featured', { defaultValue: 'Featured' })} valuePropName="checked">
              <Switch />
            </Form.Item>
          </Space>

          <Form.Item style={{ marginBottom: 0, textAlign: 'right' }}>
            <Space>
              <Button onClick={() => setRewardModal({ open: false, reward: null })}>
                {t('ui.loyalty.cancel', { defaultValue: 'Cancel' })}
              </Button>
              <AsyncButton type="primary" htmlType="submit" loading={createRewardMutation.isPending || updateRewardMutation.isPending}>
                {rewardModal.reward ? t('ui.loyalty.update', { defaultValue: 'Update' }) : t('ui.loyalty.create', { defaultValue: 'Create' })}
              </AsyncButton>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default LoyaltyRewards;
