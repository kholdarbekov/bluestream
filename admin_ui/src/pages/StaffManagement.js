import React, { useMemo, useState } from 'react';
import {
    Card, Tabs, Row, Col, Statistic, Button, Typography, Space, Table,
    Select, message, Input, Modal, Tag,
} from 'antd';
import {
    TeamOutlined, UserOutlined, CarOutlined, LinkOutlined,
    DollarOutlined, CopyOutlined,
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import staffService from '../services/staffService';

const { Title, Text } = Typography;
const { TabPane } = Tabs;
const { Option } = Select;

const StaffManagement = () => {
    const { t } = useTranslation(['staff', 'common']);
    const queryClient = useQueryClient();
    const navigate = useNavigate();

    const [activeTab, setActiveTab] = useState('overview');
    const [cashPeriod, setCashPeriod] = useState('day');
    const [inviteUserId, setInviteUserId] = useState(undefined);
    const [inviteRole, setInviteRole] = useState('delivery_driver');
    const [inviteLink, setInviteLink] = useState('');
    const [inviteModalOpen, setInviteModalOpen] = useState(false);
    const [roleDrafts, setRoleDrafts] = useState({});

    // Queries
    const { data: overviewData, isLoading: overviewLoading } = useQuery({
        queryKey: ['staffOverview'],
        queryFn: () => staffService.getStaffOverview(),
    });

    const { data: cashData, isLoading: cashLoading, refetch: refetchCash } = useQuery({
        queryKey: ['cashReconciliation', cashPeriod],
        queryFn: () => staffService.getCashReconciliation({ period: cashPeriod }),
        enabled: activeTab === 'cash',
    });

    const { data: deliveryPersonsData } = useQuery({
        queryKey: ['staffInviteDeliveryPersons'],
        queryFn: () => staffService.getDeliveryPersons({ page: 1, per_page: 200 }),
    });

    const { data: operatorsData } = useQuery({
        queryKey: ['staffInviteOperators'],
        queryFn: () => staffService.getOperators({ page: 1, per_page: 200 }),
    });

    // Mutations
    const inviteMutation = useMutation({
        mutationFn: (payload) => staffService.generateInviteLink(payload),

        onSuccess: (res) => {
            const link = res?.data?.data?.invite_link;
            if (link) {
                setInviteLink(link);
                setInviteModalOpen(true);
            }
        },

        onError: (err) => {
            const backendMessage = err?.response?.data?.message;
            message.error(backendMessage || t('common:error_occurred'));
        },
    });

    const updateRolesMutation = useMutation({
        mutationFn: ({ userId, roles }) => staffService.updateStaffRoles(userId, roles),

        onSuccess: () => {
            message.success(t('staff:roles_updated'));
            queryClient.invalidateQueries({
                queryKey: ['staffInviteDeliveryPersons'],
            });
            queryClient.invalidateQueries({
                queryKey: ['staffInviteOperators'],
            });
            queryClient.invalidateQueries({
                queryKey: ['staffOverview'],
            });
        },

        onError: (err) => {
            const backendMessage = err?.response?.data?.message;
            message.error(backendMessage || t('common:error_occurred'));
        },
    });

    const overview = overviewData?.data?.data?.overview || {};
    const cashReport = cashData?.data?.data?.report || [];
    const cashSessions = cashData?.data?.data?.sessions || [];
    const cashSummary = cashData?.data?.data?.summary || {};
    const cashGrandTotal = cashData?.data?.data?.grand_total_cash || 0;
    const deliveryPersons = deliveryPersonsData?.data?.data?.items || [];
    const operators = operatorsData?.data?.data?.items || [];

    const inviteCandidates = useMemo(() => {
        const byUserId = new Map();

        for (const person of deliveryPersons) {
            const userId = person.user_id;
            if (!userId) {
                continue;
            }
            const existing = byUserId.get(userId) || {
                user_id: userId,
                full_name: person.full_name || person.phone || `#${userId}`,
                phone: person.phone,
                roles: [],
            };
            if (!existing.roles.includes('delivery_driver')) {
                existing.roles.push('delivery_driver');
            }
            byUserId.set(userId, existing);
        }

        for (const operator of operators) {
            const userId = operator.id;
            if (!userId) {
                continue;
            }
            const existing = byUserId.get(userId) || {
                user_id: userId,
                full_name: operator.full_name || operator.phone || `#${userId}`,
                phone: operator.phone,
                roles: [],
            };
            if (!existing.roles.includes('operator')) {
                existing.roles.push('operator');
            }
            for (const role of (operator.staff_roles || [])) {
                if (!existing.roles.includes(role)) {
                    existing.roles.push(role);
                }
            }
            byUserId.set(userId, existing);
        }

        return Array.from(byUserId.values()).sort((a, b) => a.full_name.localeCompare(b.full_name));
    }, [deliveryPersons, operators]);

    const selectedInviteCandidate = inviteCandidates.find((c) => c.user_id === inviteUserId);
    const inviteRoleOptions = selectedInviteCandidate?.roles?.length
        ? selectedInviteCandidate.roles
        : ['delivery_driver', 'operator'];

    const handleInviteUserChange = (userId) => {
        setInviteUserId(userId);
        const candidate = inviteCandidates.find((item) => item.user_id === userId);
        if (!candidate || !candidate.roles?.length) {
            setInviteRole('delivery_driver');
            return;
        }
        if (!candidate.roles.includes(inviteRole)) {
            setInviteRole(candidate.roles[0]);
        }
    };

    const handleCopyLink = () => {
        navigator.clipboard.writeText(inviteLink);
        message.success(t('staff:link_copied'));
    };

    const getDraftRoles = (row) => roleDrafts[row.user_id] || row.roles || [];

    const handleRolesChange = (userId, roles) => {
        setRoleDrafts((prev) => ({ ...prev, [userId]: roles }));
    };

    const saveRoles = (row) => {
        const roles = getDraftRoles(row);
        if (!roles || roles.length === 0) {
            message.warning(t('staff:select_role'));
            return;
        }
        updateRolesMutation.mutate({ userId: row.user_id, roles });
    };

    // Cash reconciliation table columns
    const cashColumns = [
        {
            title: t('staff:driver_name'),
            dataIndex: 'driver_name',
            key: 'driver_name',
        },
        {
            title: t('staff:phone'),
            dataIndex: 'phone',
            key: 'phone',
        },
        {
            title: t('staff:delivery_count'),
            dataIndex: 'delivery_count',
            key: 'delivery_count',
        },
        {
            title: t('staff:cash_collected'),
            dataIndex: 'total_cash_collected',
            key: 'total_cash_collected',
            render: (val) => `${(val || 0).toLocaleString()} UZS`,
        },
        {
            title: t('staff:blocked_sessions', 'Blocked'),
            dataIndex: 'blocked_session_count',
            key: 'blocked_session_count',
        },
        {
            title: t('staff:mismatch_sessions', 'Mismatches'),
            dataIndex: 'mismatch_session_count',
            key: 'mismatch_session_count',
        },
        {
            title: t('staff:overdue_sessions', 'Overdue'),
            dataIndex: 'overdue_session_count',
            key: 'overdue_session_count',
        },
    ];

    const sessionColumns = [
        {
            title: t('staff:driver_name'),
            dataIndex: 'driver_name',
            key: 'driver_name',
        },
        {
            title: t('staff:session_started_at', 'Started'),
            dataIndex: 'session_started_at',
            key: 'session_started_at',
        },
        {
            title: t('staff:status', 'Status'),
            dataIndex: 'status',
            key: 'status',
            render: (value, record) => {
                let color = 'blue';
                if (record.blocked_from_cod) {
                    color = 'red';
                } else if (value === 'verified') {
                    color = 'green';
                } else if (value === 'partial') {
                    color = 'orange';
                }
                return <Tag color={color}>{value}</Tag>;
            },
        },
        {
            title: t('staff:expected_cash', 'Expected Cash'),
            dataIndex: 'expected_cash',
            key: 'expected_cash',
            render: (value) => `${(value || 0).toLocaleString()} UZS`,
        },
        {
            title: t('staff:declared_cash', 'Declared Cash'),
            dataIndex: 'declared_cash',
            key: 'declared_cash',
            render: (value) => (value == null ? '—' : `${value.toLocaleString()} UZS`),
        },
        {
            title: t('staff:verified_cash', 'Verified Cash'),
            dataIndex: 'verified_cash',
            key: 'verified_cash',
            render: (value) => (value == null ? '—' : `${value.toLocaleString()} UZS`),
        },
        {
            title: t('staff:variance', 'Variance'),
            dataIndex: 'verified_variance',
            key: 'verified_variance',
            render: (value, record) => {
                const variance = value == null ? record.declared_variance : value;
                return `${(variance || 0).toLocaleString()} UZS`;
            },
        },
    ];

    const roleColumns = [
        {
            title: t('staff:name'),
            dataIndex: 'full_name',
            key: 'full_name',
        },
        {
            title: t('staff:phone'),
            dataIndex: 'phone',
            key: 'phone',
            render: (value) => value || '—',
        },
        {
            title: t('staff:roles'),
            key: 'roles',
            render: (_, row) => (
                <Select
                    mode="multiple"
                    style={{ minWidth: 260 }}
                    value={getDraftRoles(row)}
                    onChange={(roles) => handleRolesChange(row.user_id, roles)}
                >
                    <Option value="delivery_driver">{t('staff:delivery_driver')}</Option>
                    <Option value="operator">{t('staff:operator')}</Option>
                </Select>
            ),
        },
        {
            title: t('staff:actions'),
            key: 'actions',
            render: (_, row) => (
                <Button
                    type="primary"
                    onClick={() => saveRoles(row)}
                    loading={updateRolesMutation.isPending && updateRolesMutation.variables?.userId === row.user_id}
                >
                    {t('common:save')}
                </Button>
            ),
        },
    ];

    return (
        <div>
            <Row justify="space-between" align="middle" style={{ marginBottom: 24 }}>
                <Col>
                    <Title level={3}>{t('staff:staff_management')}</Title>
                </Col>
                <Col>
                    <Space>
                        <Select
                            value={inviteUserId}
                            onChange={handleInviteUserChange}
                            style={{ width: 280 }}
                            placeholder={t('staff:select_staff_member')}
                            showSearch
                            optionFilterProp="children"
                        >
                            {inviteCandidates.map((item) => (
                                <Option key={item.user_id} value={item.user_id}>
                                    {item.full_name} {item.phone ? `(${item.phone})` : ''}
                                </Option>
                            ))}
                        </Select>
                        <Select value={inviteRole} onChange={setInviteRole} style={{ width: 180 }}>
                            {inviteRoleOptions.map((role) => (
                                <Option key={role} value={role}>
                                    {t(`staff:${role}`)}
                                </Option>
                            ))}
                        </Select>
                        <Button
                            type="primary"
                            icon={<LinkOutlined />}
                            onClick={() => {
                                if (!inviteUserId) {
                                    message.warning(t('staff:select_staff_member'));
                                    return;
                                }
                                inviteMutation.mutate({ user_id: inviteUserId, role: inviteRole });
                            }}
                            loading={inviteMutation.isPending}
                            disabled={!inviteUserId}
                        >
                            {t('staff:generate_invite')}
                        </Button>
                    </Space>
                </Col>
            </Row>

            <Tabs activeKey={activeTab} onChange={setActiveTab}>
                {/* Overview Tab */}
                <TabPane tab={t('staff:overview')} key="overview">
                    <Row gutter={[16, 16]}>
                        <Col xs={12} sm={6}>
                            <Card loading={overviewLoading}>
                                <Statistic
                                    title={t('staff:total_staff')}
                                    value={overview.total_staff || 0}
                                    prefix={<TeamOutlined />}
                                />
                            </Card>
                        </Col>
                        <Col xs={12} sm={6}>
                            <Card loading={overviewLoading}>
                                <Statistic
                                    title={t('staff:delivery_persons')}
                                    value={overview.total_delivery_persons || 0}
                                    prefix={<CarOutlined />}
                                />
                            </Card>
                        </Col>
                        <Col xs={12} sm={6}>
                            <Card loading={overviewLoading}>
                                <Statistic
                                    title={t('staff:operators')}
                                    value={overview.total_operators || 0}
                                    prefix={<UserOutlined />}
                                />
                            </Card>
                        </Col>
                        <Col xs={12} sm={6}>
                            <Card loading={overviewLoading}>
                                <Statistic
                                    title={t('staff:active_drivers')}
                                    value={overview.active_drivers || 0}
                                    valueStyle={{ color: '#52c41a' }}
                                />
                            </Card>
                        </Col>
                    </Row>

                    <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
                        <Col xs={12} sm={6}>
                            <Card loading={overviewLoading}>
                                <Statistic
                                    title={t('staff:orders_today')}
                                    value={overview.orders_today || 0}
                                />
                            </Card>
                        </Col>
                        <Col xs={12} sm={6}>
                            <Card loading={overviewLoading}>
                                <Statistic
                                    title={t('staff:pending_orders')}
                                    value={overview.pending_orders || 0}
                                    valueStyle={{ color: '#faad14' }}
                                />
                            </Card>
                        </Col>
                        <Col xs={12} sm={6}>
                            <Card loading={overviewLoading}>
                                <Statistic
                                    title={t('staff:active_deliveries')}
                                    value={overview.active_deliveries || 0}
                                    valueStyle={{ color: '#1890ff' }}
                                />
                            </Card>
                        </Col>
                        <Col xs={12} sm={6}>
                            <Card loading={overviewLoading}>
                                <Statistic
                                    title={t('staff:unassigned_deliveries')}
                                    value={overview.unassigned_deliveries || 0}
                                    valueStyle={{ color: overview.unassigned_deliveries > 0 ? '#ff4d4f' : undefined }}
                                />
                            </Card>
                        </Col>
                    </Row>
                </TabPane>

                {/* Cash Reconciliation Tab */}
                <TabPane tab={t('staff:cash_reconciliation')} key="cash">
                    <Card>
                        <Row gutter={[16, 16]} align="middle" style={{ marginBottom: 16 }}>
                            <Col>
                                <Select value={cashPeriod} onChange={setCashPeriod} style={{ width: 140 }}>
                                    <Option value="day">{t('staff:today')}</Option>
                                    <Option value="week">{t('staff:this_week')}</Option>
                                    <Option value="month">{t('staff:this_month')}</Option>
                                </Select>
                            </Col>
                            <Col>
                                <Button onClick={() => refetchCash()}>
                                    {t('common:refresh')}
                                </Button>
                            </Col>
                            <Col>
                                <Button type="primary" onClick={() => navigate('/delivery/reports')}>
                                    {t('staff:open_reconciliation_workbench', 'Open Delivery Reports Workbench')}
                                </Button>
                            </Col>
                            <Col flex="auto" style={{ textAlign: 'right' }}>
                                <Statistic
                                    title={t('staff:grand_total')}
                                    value={cashGrandTotal}
                                    prefix={<DollarOutlined />}
                                    suffix="UZS"
                                    valueStyle={{ color: '#52c41a' }}
                                />
                            </Col>
                        </Row>

                        <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
                            <Col xs={24} sm={8} md={6}>
                                <Statistic
                                    title={t('staff:blocked_sessions', 'Blocked Sessions')}
                                    value={cashSummary.blocked_session_count || 0}
                                    valueStyle={{ color: '#ff4d4f' }}
                                />
                            </Col>
                            <Col xs={24} sm={8} md={6}>
                                <Statistic
                                    title={t('staff:mismatch_sessions', 'Mismatches')}
                                    value={cashSummary.mismatch_session_count || 0}
                                    valueStyle={{ color: '#faad14' }}
                                />
                            </Col>
                            <Col xs={24} sm={8} md={6}>
                                <Statistic
                                    title={t('staff:overdue_sessions', 'Overdue')}
                                    value={cashSummary.overdue_session_count || 0}
                                    valueStyle={{ color: '#cf1322' }}
                                />
                            </Col>
                            <Col xs={24} sm={8} md={6}>
                                <Statistic
                                    title={t('staff:open_sessions', 'Open Sessions')}
                                    value={cashSummary.open_session_count || 0}
                                />
                            </Col>
                        </Row>

                        <Table
                            columns={cashColumns}
                            dataSource={cashReport}
                            rowKey="driver_id"
                            loading={cashLoading}
                            pagination={false}
                            scroll={{ x: 900 }}
                        />

                        <Table
                            style={{ marginTop: 24 }}
                            columns={sessionColumns}
                            dataSource={cashSessions}
                            rowKey="id"
                            loading={cashLoading}
                            pagination={false}
                            scroll={{ x: 960 }}
                        />
                    </Card>
                </TabPane>

                <TabPane tab={t('staff:roles')} key="roles">
                    <Card>
                        <Table
                            columns={roleColumns}
                            dataSource={inviteCandidates}
                            rowKey="user_id"
                            pagination={false}
                            scroll={{ x: 760 }}
                        />
                    </Card>
                </TabPane>
            </Tabs>

            {/* Invite Link Modal */}
            <Modal
                title={t('staff:invite_link')}
                open={inviteModalOpen}
                onCancel={() => setInviteModalOpen(false)}
                footer={[
                    <Button key="copy" type="primary" icon={<CopyOutlined />} onClick={handleCopyLink}>
                        {t('staff:copy_link')}
                    </Button>,
                    <Button key="close" onClick={() => setInviteModalOpen(false)}>
                        {t('common:close')}
                    </Button>,
                ]}
            >
                <Space direction="vertical" style={{ width: '100%' }}>
                    <Text>{t('staff:share_invite_description')}</Text>
                    <Input
                        value={inviteLink}
                        readOnly
                        addonAfter={
                            <CopyOutlined onClick={handleCopyLink} style={{ cursor: 'pointer' }} />
                        }
                    />
                </Space>
            </Modal>
        </div>
    );
};

export default StaffManagement;
