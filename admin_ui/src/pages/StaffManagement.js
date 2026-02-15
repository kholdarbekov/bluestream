import React, { useMemo, useState } from 'react';
import {
    Card, Tabs, Row, Col, Statistic, Button, Typography, Space, Table,
    Select, message, Input, Modal,
} from 'antd';
import {
    TeamOutlined, UserOutlined, CarOutlined, LinkOutlined,
    DollarOutlined, CopyOutlined,
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient } from 'react-query';
import { useTranslation } from 'react-i18next';
import staffService from '../services/staffService';

const { Title, Text } = Typography;
const { TabPane } = Tabs;
const { Option } = Select;

const StaffManagement = () => {
    const { t } = useTranslation(['staff', 'common']);
    const queryClient = useQueryClient();

    const [activeTab, setActiveTab] = useState('overview');
    const [cashPeriod, setCashPeriod] = useState('day');
    const [inviteUserId, setInviteUserId] = useState(undefined);
    const [inviteRole, setInviteRole] = useState('delivery_driver');
    const [inviteLink, setInviteLink] = useState('');
    const [inviteModalOpen, setInviteModalOpen] = useState(false);
    const [roleDrafts, setRoleDrafts] = useState({});

    // Queries
    const { data: overviewData, isLoading: overviewLoading } = useQuery(
        'staffOverview',
        () => staffService.getStaffOverview()
    );

    const { data: cashData, isLoading: cashLoading, refetch: refetchCash } = useQuery(
        ['cashReconciliation', cashPeriod],
        () => staffService.getCashReconciliation({ period: cashPeriod }),
        { enabled: activeTab === 'cash' }
    );

    const { data: deliveryPersonsData } = useQuery(
        'staffInviteDeliveryPersons',
        () => staffService.getDeliveryPersons({ page: 1, per_page: 200 })
    );

    const { data: operatorsData } = useQuery(
        'staffInviteOperators',
        () => staffService.getOperators({ page: 1, per_page: 200 })
    );

    // Mutations
    const inviteMutation = useMutation(
        (payload) => staffService.generateInviteLink(payload),
        {
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
        }
    );

    const updateRolesMutation = useMutation(
        ({ userId, roles }) => staffService.updateStaffRoles(userId, roles),
        {
            onSuccess: () => {
                message.success(t('staff:roles_updated'));
                queryClient.invalidateQueries('staffInviteDeliveryPersons');
                queryClient.invalidateQueries('staffInviteOperators');
                queryClient.invalidateQueries('staffOverview');
            },
            onError: (err) => {
                const backendMessage = err?.response?.data?.message;
                message.error(backendMessage || t('common:error_occurred'));
            },
        }
    );

    const overview = overviewData?.data?.data?.overview || {};
    const cashReport = cashData?.data?.data?.report || [];
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
                    loading={updateRolesMutation.isLoading && updateRolesMutation.variables?.userId === row.user_id}
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
                            loading={inviteMutation.isLoading}
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

                        <Table
                            columns={cashColumns}
                            dataSource={cashReport}
                            rowKey="driver_id"
                            loading={cashLoading}
                            pagination={false}
                            scroll={{ x: 600 }}
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
