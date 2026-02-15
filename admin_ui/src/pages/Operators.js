import React, { useState } from 'react';
import {
    Card, Table, Tag, Space, Button, Input, Select, Row, Col,
    Statistic, Drawer, Descriptions, Typography, message, Modal, Form,
} from 'antd';
import {
    SearchOutlined, UserOutlined, PhoneOutlined, ReloadOutlined,
    ShoppingCartOutlined, PlusOutlined, EditOutlined, LinkOutlined, CopyOutlined,
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient } from 'react-query';
import { useTranslation } from 'react-i18next';
import staffService from '../services/staffService';

const { Title, Text } = Typography;
const { Option } = Select;

const Operators = () => {
    const { t } = useTranslation(['staff', 'common']);
    const queryClient = useQueryClient();
    const [form] = Form.useForm();

    const [page, setPage] = useState(1);
    const [perPage] = useState(20);
    const [search, setSearch] = useState('');
    const [statusFilter, setStatusFilter] = useState(undefined);
    const [selectedOperator, setSelectedOperator] = useState(null);
    const [drawerOpen, setDrawerOpen] = useState(false);
    const [editorOpen, setEditorOpen] = useState(false);
    const [editingOperator, setEditingOperator] = useState(null);
    const [inviteModalOpen, setInviteModalOpen] = useState(false);
    const [inviteLink, setInviteLink] = useState('');

    const { data, isLoading, refetch } = useQuery(
        ['staffOperators', page, perPage, search, statusFilter],
        () =>
            staffService.getOperators({
                page,
                per_page: perPage,
                search: search || undefined,
                status: statusFilter,
            }),
        { keepPreviousData: true }
    );

    const saveMutation = useMutation(
        (payload) => {
            if (editingOperator?.id) {
                return staffService.updateOperator(editingOperator.id, payload);
            }
            return staffService.createOperator(payload);
        },
        {
            onSuccess: () => {
                message.success(editingOperator ? t('staff:operator_updated') : t('staff:operator_created'));
                setEditorOpen(false);
                setEditingOperator(null);
                form.resetFields();
                queryClient.invalidateQueries('staffOperators');
                queryClient.invalidateQueries('staffInviteOperators');
            },
            onError: (err) => {
                const backendMessage = err?.response?.data?.message;
                message.error(backendMessage || t('common:error_occurred'));
            },
        }
    );

    const inviteMutation = useMutation(
        ({ userId }) => staffService.generateInviteLink({ user_id: userId, role: 'operator' }),
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

    const items = data?.data?.data?.items || [];
    const total = data?.data?.meta?.total || 0;
    const summary = data?.data?.meta?.summary || {};

    const openCreateModal = () => {
        setEditingOperator(null);
        form.setFieldsValue({
            status: 'active',
            staff_roles: ['operator'],
        });
        setEditorOpen(true);
    };

    const openEditModal = (record) => {
        setEditingOperator(record);
        form.setFieldsValue({
            first_name: record.first_name,
            last_name: record.last_name,
            phone: record.phone,
            email: record.email,
            status: record.status || 'active',
            staff_roles: record.staff_roles?.length ? record.staff_roles : ['operator'],
        });
        setEditorOpen(true);
    };

    const handleSubmit = (values) => {
        const payload = {
            ...values,
            first_name: values.first_name?.trim() || null,
            last_name: values.last_name?.trim() || null,
            phone: values.phone?.trim() || null,
            email: values.email?.trim() || null,
            staff_roles: values.staff_roles || ['operator'],
        };
        saveMutation.mutate(payload);
    };

    const handleGenerateInvite = (record) => {
        inviteMutation.mutate({ userId: record.id });
    };

    const handleCopyInvite = () => {
        navigator.clipboard.writeText(inviteLink);
        message.success(t('staff:link_copied'));
    };

    const columns = [
        {
            title: t('staff:name'),
            dataIndex: 'full_name',
            key: 'full_name',
            render: (text, record) => (
                <Space>
                    <UserOutlined />
                    <a onClick={() => { setSelectedOperator(record); setDrawerOpen(true); }}>
                        {text || `${record.first_name || ''} ${record.last_name || ''}`.trim() || '—'}
                    </a>
                </Space>
            ),
        },
        {
            title: t('staff:phone'),
            dataIndex: 'phone',
            key: 'phone',
            render: (text) => (
                <Space>
                    <PhoneOutlined />
                    {text || '—'}
                </Space>
            ),
        },
        {
            title: t('staff:status'),
            dataIndex: 'status',
            key: 'status',
            render: (status) => (
                <Tag color={status === 'active' ? 'green' : 'red'}>
                    {status === 'active' ? t('staff:active') : t('staff:inactive')}
                </Tag>
            ),
        },
        {
            title: t('staff:roles'),
            key: 'staff_roles',
            render: (_, record) => (
                <Space>
                    {(record.staff_roles || []).map((role) => (
                        <Tag key={role} color={role === 'operator' ? 'blue' : 'purple'}>
                            {t(`staff:${role}`)}
                        </Tag>
                    ))}
                    {(!record.staff_roles || record.staff_roles.length === 0) && (
                        <Tag color="blue">{record.role}</Tag>
                    )}
                </Space>
            ),
        },
        {
            title: t('staff:orders_today'),
            dataIndex: 'orders_today',
            key: 'orders_today',
            render: (val) => (
                <Space>
                    <ShoppingCartOutlined />
                    {val || 0}
                </Space>
            ),
        },
        {
            title: t('staff:total_orders'),
            dataIndex: 'total_orders_created',
            key: 'total_orders_created',
            render: (val) => val || 0,
        },
        {
            title: t('staff:last_login'),
            dataIndex: 'last_login',
            key: 'last_login',
            render: (val) =>
                val ? new Date(val).toLocaleString() : <Text type="secondary">—</Text>,
        },
        {
            title: t('staff:actions'),
            key: 'actions',
            render: (_, record) => (
                <Space>
                    <Button
                        size="small"
                        onClick={() => {
                            setSelectedOperator(record);
                            setDrawerOpen(true);
                        }}
                    >
                        {t('common:view')}
                    </Button>
                    <Button size="small" icon={<EditOutlined />} onClick={() => openEditModal(record)}>
                        {t('common:edit')}
                    </Button>
                    <Button
                        size="small"
                        icon={<LinkOutlined />}
                        loading={inviteMutation.isLoading}
                        onClick={() => handleGenerateInvite(record)}
                    >
                        {t('staff:invite')}
                    </Button>
                </Space>
            ),
        },
    ];

    return (
        <div>
            <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
                <Col>
                    <Title level={3} style={{ margin: 0 }}>{t('staff:operators')}</Title>
                </Col>
                <Col>
                    <Button type="primary" icon={<PlusOutlined />} onClick={openCreateModal}>
                        {t('staff:add_operator')}
                    </Button>
                </Col>
            </Row>

            <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
                <Col xs={12} sm={8}>
                    <Card>
                        <Statistic
                            title={t('staff:total_operators')}
                            value={summary.total_operators || 0}
                            prefix={<UserOutlined />}
                        />
                    </Card>
                </Col>
                <Col xs={12} sm={8}>
                    <Card>
                        <Statistic
                            title={t('staff:active_operators')}
                            value={summary.active_operators || 0}
                            valueStyle={{ color: '#52c41a' }}
                        />
                    </Card>
                </Col>
            </Row>

            <Card style={{ marginBottom: 16 }}>
                <Row gutter={[16, 16]} align="middle">
                    <Col xs={24} sm={10}>
                        <Input
                            placeholder={t('staff:search_placeholder')}
                            prefix={<SearchOutlined />}
                            value={search}
                            onChange={(e) => { setSearch(e.target.value); setPage(1); }}
                            allowClear
                        />
                    </Col>
                    <Col xs={12} sm={6}>
                        <Select
                            placeholder={t('staff:status')}
                            value={statusFilter}
                            onChange={(val) => { setStatusFilter(val); setPage(1); }}
                            allowClear
                            style={{ width: '100%' }}
                        >
                            <Option value="active">{t('staff:active')}</Option>
                            <Option value="inactive">{t('staff:inactive')}</Option>
                        </Select>
                    </Col>
                    <Col>
                        <Button icon={<ReloadOutlined />} onClick={() => refetch()}>
                            {t('common:refresh')}
                        </Button>
                    </Col>
                </Row>
            </Card>

            <Card>
                <Table
                    columns={columns}
                    dataSource={items}
                    rowKey="id"
                    loading={isLoading}
                    pagination={{
                        current: page,
                        pageSize: perPage,
                        total,
                        onChange: setPage,
                        showSizeChanger: false,
                        showTotal: (totalCount) => `${t('common:total')}: ${totalCount}`,
                    }}
                    scroll={{ x: 900 }}
                />
            </Card>

            <Drawer
                title={selectedOperator?.full_name || t('staff:operator_details')}
                open={drawerOpen}
                onClose={() => { setDrawerOpen(false); setSelectedOperator(null); }}
                width={480}
            >
                {selectedOperator && (
                    <Space direction="vertical" size="large" style={{ width: '100%' }}>
                        <Descriptions column={1} bordered size="small">
                            <Descriptions.Item label={t('staff:name')}>
                                {selectedOperator.full_name || `${selectedOperator.first_name || ''} ${selectedOperator.last_name || ''}`.trim()}
                            </Descriptions.Item>
                            <Descriptions.Item label={t('staff:phone')}>{selectedOperator.phone || '—'}</Descriptions.Item>
                            <Descriptions.Item label={t('staff:email')}>{selectedOperator.email || '—'}</Descriptions.Item>
                            <Descriptions.Item label={t('staff:status')}>
                                <Tag color={selectedOperator.status === 'active' ? 'green' : 'red'}>
                                    {selectedOperator.status}
                                </Tag>
                            </Descriptions.Item>
                            <Descriptions.Item label={t('staff:roles')}>
                                <Space>
                                    {(selectedOperator.staff_roles || []).map((role) => (
                                        <Tag key={role}>{t(`staff:${role}`)}</Tag>
                                    ))}
                                </Space>
                            </Descriptions.Item>
                            <Descriptions.Item label={t('staff:orders_today')}>{selectedOperator.orders_today}</Descriptions.Item>
                            <Descriptions.Item label={t('staff:total_orders')}>{selectedOperator.total_orders_created}</Descriptions.Item>
                            <Descriptions.Item label={t('staff:last_login')}>
                                {selectedOperator.last_login ? new Date(selectedOperator.last_login).toLocaleString() : '—'}
                            </Descriptions.Item>
                            <Descriptions.Item label={t('staff:joined')}>
                                {selectedOperator.created_at ? new Date(selectedOperator.created_at).toLocaleDateString() : '—'}
                            </Descriptions.Item>
                        </Descriptions>

                        <Space>
                            <Button icon={<EditOutlined />} onClick={() => openEditModal(selectedOperator)}>
                                {t('common:edit')}
                            </Button>
                            <Button icon={<LinkOutlined />} onClick={() => handleGenerateInvite(selectedOperator)}>
                                {t('staff:invite')}
                            </Button>
                        </Space>
                    </Space>
                )}
            </Drawer>

            <Modal
                title={editingOperator ? t('staff:edit_operator') : t('staff:add_operator')}
                open={editorOpen}
                onCancel={() => {
                    setEditorOpen(false);
                    setEditingOperator(null);
                    form.resetFields();
                }}
                footer={null}
                destroyOnClose
            >
                <Form form={form} layout="vertical" onFinish={handleSubmit}>
                    <Form.Item name="first_name" label={t('staff:first_name')}>
                        <Input />
                    </Form.Item>
                    <Form.Item name="last_name" label={t('staff:last_name')}>
                        <Input />
                    </Form.Item>
                    <Form.Item
                        name="phone"
                        label={t('staff:phone')}
                        rules={[{ required: !editingOperator, message: t('staff:phone') }]}
                    >
                        <Input />
                    </Form.Item>
                    <Form.Item name="email" label={t('staff:email')}>
                        <Input />
                    </Form.Item>
                    <Form.Item name="status" label={t('staff:status')}>
                        <Select allowClear>
                            <Option value="active">{t('staff:active')}</Option>
                            <Option value="inactive">{t('staff:inactive')}</Option>
                            <Option value="banned">{t('staff:banned')}</Option>
                        </Select>
                    </Form.Item>
                    <Form.Item name="staff_roles" label={t('staff:roles')}>
                        <Select mode="multiple">
                            <Option value="operator">{t('staff:operator')}</Option>
                            <Option value="delivery_driver">{t('staff:delivery_driver')}</Option>
                        </Select>
                    </Form.Item>
                    <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
                        <Button onClick={() => setEditorOpen(false)}>
                            {t('common:cancel')}
                        </Button>
                        <Button type="primary" htmlType="submit" loading={saveMutation.isLoading}>
                            {t('common:save')}
                        </Button>
                    </Space>
                </Form>
            </Modal>

            <Modal
                title={t('staff:invite_link')}
                open={inviteModalOpen}
                onCancel={() => setInviteModalOpen(false)}
                footer={[
                    <Button key="copy" type="primary" icon={<CopyOutlined />} onClick={handleCopyInvite}>
                        {t('staff:copy_link')}
                    </Button>,
                    <Button key="close" onClick={() => setInviteModalOpen(false)}>
                        {t('common:close')}
                    </Button>,
                ]}
            >
                <Space direction="vertical" style={{ width: '100%' }}>
                    <Text>{t('staff:share_invite_description')}</Text>
                    <Input value={inviteLink} readOnly />
                </Space>
            </Modal>
        </div>
    );
};

export default Operators;
