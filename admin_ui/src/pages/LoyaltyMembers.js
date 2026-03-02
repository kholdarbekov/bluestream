import React, { useMemo, useState } from 'react';
import {
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Input,
  List,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message
} from 'antd';
import {
  ExportOutlined,
  EyeOutlined,
  SearchOutlined,
  StarOutlined,
  TrophyOutlined,
  UserOutlined
} from '@ant-design/icons';
import { useQuery } from 'react-query';
import { useTranslation } from 'react-i18next';
import adminService from '../services/adminService';
import exportUtils from '../utils/exportUtils';
import { formatDate, formatDateTime } from '../utils/dateUtils';

const { Text } = Typography;

const LoyaltyMembers = () => {
  const { t } = useTranslation('loyalty');
  const [searchText, setSearchText] = useState('');
  const [programId, setProgramId] = useState();
  const [pagination, setPagination] = useState({ page: 1, per_page: 20 });
  const [selectedMemberId, setSelectedMemberId] = useState(null);

  const membersQuery = useQuery(
    ['loyalty-members', pagination, searchText, programId],
    () => adminService.getLoyaltyMembers({
      page: pagination.page,
      per_page: pagination.per_page,
      search: searchText,
      program_id: programId,
    }),
    { keepPreviousData: true }
  );

  const programsQuery = useQuery(
    ['loyalty-program-options'],
    () => adminService.getLoyaltyPrograms({ page: 1, per_page: 100 }),
    { keepPreviousData: true }
  );

  const memberDetailQuery = useQuery(
    ['loyalty-member-detail', selectedMemberId],
    () => adminService.getLoyaltyMember(selectedMemberId),
    { enabled: Boolean(selectedMemberId) }
  );

  const summary = membersQuery.data?.summary || {};
  const members = membersQuery.data?.items || [];
  const totalMembers = membersQuery.data?.total || 0;
  const programs = programsQuery.data?.items || [];

  const columns = useMemo(() => ([
    {
      title: t('ui.loyalty.customer', { defaultValue: 'Customer' }),
      dataIndex: 'customer_name',
      key: 'customer_name',
      render: (_, record) => (
        <div>
          <div style={{ fontWeight: 600 }}>{record.customer_name}</div>
          <Text type="secondary">{record.customer_email || record.customer_phone || '-'}</Text>
        </div>
      )
    },
    {
      title: t('ui.loyalty.program', { defaultValue: 'Program' }),
      dataIndex: 'program_name',
      key: 'program_name',
      render: (value) => value || '-'
    },
    {
      title: t('ui.loyalty.tier', { defaultValue: 'Tier' }),
      dataIndex: 'current_tier',
      key: 'current_tier',
      width: 140,
      render: (value) => <Tag color="gold">{value || 'Bronze'}</Tag>
    },
    {
      title: t('ui.loyalty.current_points', { defaultValue: 'Current Points' }),
      dataIndex: 'current_balance',
      key: 'current_balance',
      width: 160,
      render: (value) => `${value || 0} pts`
    },
    {
      title: t('ui.loyalty.total_earned', { defaultValue: 'Total Earned' }),
      dataIndex: 'total_earned',
      key: 'total_earned',
      width: 160,
      render: (value) => `${value || 0} pts`
    },
    {
      title: t('ui.loyalty.last_activity', { defaultValue: 'Last Activity' }),
      dataIndex: 'last_activity_at',
      key: 'last_activity_at',
      width: 180,
      render: (value) => value ? formatDateTime(value) : '-'
    },
    {
      title: t('ui.loyalty.actions', { defaultValue: 'Actions' }),
      key: 'actions',
      width: 90,
      render: (_, record) => (
        <Button
          type="text"
          icon={<EyeOutlined />}
          onClick={() => setSelectedMemberId(record.user_id)}
        />
      )
    }
  ]), [t]);

  const handleExport = async () => {
    const result = await exportUtils.exportLoyaltyMembers({
      search: searchText,
      program_id: programId,
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
              title={t('ui.loyalty.total_members', { defaultValue: 'Total Members' })}
              value={summary.total_members || totalMembers}
              prefix={<UserOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} md={8}>
          <Card>
            <Statistic
              title={t('ui.loyalty.points_distributed', { defaultValue: 'Points In Circulation' })}
              value={summary.total_points_in_circulation || 0}
              prefix={<StarOutlined />}
            />
          </Card>
        </Col>
        <Col xs={24} md={8}>
          <Card>
            <Statistic
              title={t('ui.loyalty.avg_points_per_member', { defaultValue: 'Average Points per Member' })}
              value={summary.average_points_balance || 0}
              prefix={<TrophyOutlined />}
            />
          </Card>
        </Col>
      </Row>

      <Card>
        <div className="table-actions">
          <Space wrap>
            <Input
              allowClear
              prefix={<SearchOutlined />}
              placeholder={t('ui.loyalty.search_members', { defaultValue: 'Search members' })}
              style={{ width: 260 }}
              value={searchText}
              onChange={(event) => {
                setSearchText(event.target.value);
                setPagination((current) => ({ ...current, page: 1 }));
              }}
            />
            <Select
              allowClear
              placeholder={t('ui.loyalty.program', { defaultValue: 'Program' })}
              style={{ width: 220 }}
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
          </Space>

          <Space>
            <Button icon={<ExportOutlined />} onClick={handleExport}>
              {t('ui.loyalty.export_members', { defaultValue: 'Export Members' })}
            </Button>
          </Space>
        </div>

        <Table
          rowKey="id"
          columns={columns}
          dataSource={members}
          loading={membersQuery.isLoading}
          locale={{
            emptyText: (
              <Empty description={t('ui.loyalty.no_members', { defaultValue: 'No loyalty members found' })} />
            )
          }}
          pagination={{
            current: pagination.page,
            pageSize: pagination.per_page,
            total: totalMembers,
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
        open={Boolean(selectedMemberId)}
        width={720}
        title={memberDetailQuery.data?.member?.customer_name || t('ui.loyalty.member_details', { defaultValue: 'Member Details' })}
        onClose={() => setSelectedMemberId(null)}
      >
        {memberDetailQuery.isLoading ? null : memberDetailQuery.data?.member ? (
          <Space direction="vertical" size={24} style={{ width: '100%' }}>
            <Descriptions bordered size="small" column={2}>
              <Descriptions.Item label={t('ui.loyalty.program', { defaultValue: 'Program' })}>
                {memberDetailQuery.data.member.program_name || '-'}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.loyalty.tier', { defaultValue: 'Tier' })}>
                {memberDetailQuery.data.member.current_tier || '-'}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.loyalty.current_points', { defaultValue: 'Current Points' })}>
                {memberDetailQuery.data.member.current_balance || 0}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.loyalty.total_earned', { defaultValue: 'Total Earned' })}>
                {memberDetailQuery.data.member.total_earned || 0}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.loyalty.member_since', { defaultValue: 'Member Since' })}>
                {formatDate(memberDetailQuery.data.member.member_since)}
              </Descriptions.Item>
              <Descriptions.Item label={t('ui.loyalty.last_activity', { defaultValue: 'Last Activity' })}>
                {memberDetailQuery.data.member.last_activity_at ? formatDateTime(memberDetailQuery.data.member.last_activity_at) : '-'}
              </Descriptions.Item>
            </Descriptions>

            <Card
              size="small"
              title={t('ui.loyalty.recent_activity', { defaultValue: 'Recent Transactions' })}
            >
              <List
                locale={{
                  emptyText: t('ui.loyalty.no_recent_activity', { defaultValue: 'No recent transactions' })
                }}
                dataSource={memberDetailQuery.data.recent_transactions || []}
                renderItem={(item) => (
                  <List.Item>
                    <List.Item.Meta
                      title={item.description}
                      description={item.created_at ? formatDateTime(item.created_at) : '-'}
                    />
                    <Tag color={item.points >= 0 ? 'green' : 'red'}>
                      {item.points}
                    </Tag>
                  </List.Item>
                )}
              />
            </Card>

            <Card
              size="small"
              title={t('ui.loyalty.recent_redemptions', { defaultValue: 'Recent Redemptions' })}
            >
              <List
                locale={{
                  emptyText: t('ui.loyalty.no_recent_redemptions', { defaultValue: 'No recent redemptions' })
                }}
                dataSource={memberDetailQuery.data.recent_redemptions || []}
                renderItem={(item) => (
                  <List.Item>
                    <List.Item.Meta
                      title={item.description}
                      description={item.created_at ? formatDateTime(item.created_at) : '-'}
                    />
                    <Tag color="volcano">{item.points}</Tag>
                  </List.Item>
                )}
              />
            </Card>
          </Space>
        ) : (
          <Empty description={t('ui.loyalty.member_not_found', { defaultValue: 'Member not found' })} />
        )}
      </Drawer>
    </div>
  );
};

export default LoyaltyMembers;
