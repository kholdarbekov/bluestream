import React, { useState } from 'react';
import {
    Card, Table, Tag, Space, Button, Input, Select, Row, Col,
    Statistic, Switch, Drawer, Descriptions, Typography, message,
    Modal, Form, InputNumber,
} from 'antd';
import {
    SearchOutlined, UserOutlined, PhoneOutlined, ReloadOutlined, PlusOutlined, EditOutlined, LinkOutlined, CopyOutlined,
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import staffService from '../services/staffService';
import api from '../services/api';

const { Title, Text } = Typography;
const { Option } = Select;

// Mirrors business_app/models/sales.py EMPLOYMENT_TYPES.
const EMPLOYMENT_TYPES = ['employee', 'contractor'];

const SalesAgents = () => {
    const { t, i18n } = useTranslation(['sales_agents', 'common']);
    const queryClient = useQueryClient();
    const [form] = Form.useForm();

    const [page, setPage] = useState(1);
    const [perPage] = useState(20);
    const [search, setSearch] = useState('');
    const [statusFilter, setStatusFilter] = useState(undefined);

    const [selectedAgent, setSelectedAgent] = useState(null);
    const [drawerOpen, setDrawerOpen] = useState(false);
    const [editorOpen, setEditorOpen] = useState(false);
    const [editingAgent, setEditingAgent] = useState(null);
    const [inviteModalOpen, setInviteModalOpen] = useState(false);
    const [inviteLink, setInviteLink] = useState('');

    const { data, isLoading, refetch } = useQuery({
        queryKey: ['salesAgents', page, perPage, search, statusFilter],
        queryFn: () => staffService.getSalesAgents({ page, per_page: perPage, search: search || undefined, status: statusFilter }),
        placeholderData: keepPreviousData,
    });

    const { data: districtsData } = useQuery({
        queryKey: ['geoDistricts', i18n?.language],
        queryFn: () => api.get(`/addresses/geo-config?lang=${i18n?.language || 'en'}`),
        staleTime: 3600_000,
    });
    const districts = districtsData?.data?.data?.districts || [];

    const saveMutation = useMutation({
        mutationFn: (payload) => (editingAgent?.user_id
            ? staffService.updateSalesAgent(editingAgent.user_id, payload)
            : staffService.createSalesAgent(payload)),
        onSuccess: () => {
            message.success(editingAgent ? t('sales_agents:agent_updated', 'Sales agent updated') : t('sales_agents:agent_created', 'Sales agent created'));
            setEditorOpen(false);
            setEditingAgent(null);
            form.resetFields();
            queryClient.invalidateQueries({ queryKey: ['salesAgents'] });
        },
        onError: (err) => message.error(err?.response?.data?.message || t('ui.common.error_occurred', 'An error occurred')),
    });

    const activeMutation = useMutation({
        mutationFn: ({ userId, isActive }) => staffService.setSalesAgentActive(userId, isActive),
        onSuccess: () => {
            message.success(t('sales_agents:agent_updated', 'Sales agent updated'));
            queryClient.invalidateQueries({ queryKey: ['salesAgents'] });
        },
        onError: (err) => message.error(err?.response?.data?.message || t('ui.common.error_occurred', 'An error occurred')),
    });

    const inviteMutation = useMutation({
        mutationFn: ({ userId }) => staffService.generateInviteLink({ user_id: userId, role: 'sales_agent' }),
        onSuccess: (res) => {
            const link = res?.data?.data?.invite_link;
            if (link) {
                setInviteLink(link);
                setInviteModalOpen(true);
            }
        },
        onError: (err) => message.error(err?.response?.data?.message || t('ui.common.error_occurred', 'An error occurred')),
    });

    const items = data?.data?.data?.items || [];
    const total = data?.data?.meta?.total || 0;
    const summary = data?.data?.meta?.summary || {};

    const openCreateModal = () => {
        setEditingAgent(null);
        form.setFieldsValue({
            full_name: undefined,
            phone: undefined,
            email: undefined,
            districts: [],
            employment_type: 'employee',
            weekly_new_outlet_target: null,
            notes: undefined,
        });
        setEditorOpen(true);
    };

    const openEditModal = (record) => {
        setEditingAgent(record);
        form.setFieldsValue({
            full_name: record.full_name,
            phone: record.phone,
            email: record.email,
            districts: record.districts || [],
            weekly_new_outlet_target: record.weekly_new_outlet_target,
            employment_type: record.employment_type || 'employee',
            notes: record.notes,
        });
        setEditorOpen(true);
    };

    // `is_active` is deliberately absent: PUT /admin/staff/sales-agents/<id>/active
    // is the only door onto it (it is the only one that writes the
    // `sales_agent_set_active` staff-activity entry).
    const handleEditorSubmit = (values) => {
        const email = values.email?.trim() || null;
        saveMutation.mutate({
            full_name: values.full_name?.trim(),
            phone: values.phone?.trim(),
            // Editing names the agent by id, so an emptied input there still clears the column.
            // Creating does not: the phone may match an account that already exists, and the
            // backend attaches to its owner. A blank input then means "I have none to add", so
            // omit the key and let `exclude_unset` drop it rather than blanking their email.
            ...(email === null && !editingAgent ? {} : { email }),
            districts: values.districts || [],
            weekly_new_outlet_target: values.weekly_new_outlet_target ?? null,
            employment_type: values.employment_type || null,
            notes: values.notes?.trim() || null,
        });
    };

    const handleCopyInvite = () => {
        navigator.clipboard.writeText(inviteLink);
        message.success(t('sales_agents:link_copied', 'Link copied'));
    };

    const columns = [
        {
            title: t('sales_agents:contact_name', 'Name'),
            dataIndex: 'full_name',
            key: 'full_name',
            render: (text, record) => (
                <Space>
                    <UserOutlined />
                    <a onClick={() => { setSelectedAgent(record); setDrawerOpen(true); }}>{text}</a>
                </Space>
            ),
        },
        {
            title: t('sales_agents:phone', 'Phone'),
            dataIndex: 'phone',
            key: 'phone',
            render: (text) => (<Space><PhoneOutlined />{text}</Space>),
        },
        {
            title: t('sales_agents:districts', 'Districts'),
            dataIndex: 'districts',
            key: 'districts',
            render: (value) => (value || []).map((d) => <Tag key={d}>{d}</Tag>),
        },
        {
            title: t('sales_agents:outlets_assigned_active', 'Outlets (assigned / active)'),
            key: 'outlets',
            render: (_, record) => `${record.outlets_assigned} / ${record.outlets_active}`,
        },
        {
            title: t('sales_agents:agents.visits_today', 'Visits today'),
            key: 'visits_today',
            render: (_, record) => record.visits_today ?? 0,
        },
        {
            title: t('sales_agents:agents.orders_today', 'Orders today'),
            key: 'orders_today',
            render: (_, record) => record.orders_today ?? 0,
        },
        {
            title: t('sales_agents:agent_active', 'Active'),
            key: 'is_active',
            render: (_, record) => (
                <Switch
                    checked={Boolean(record.is_active)}
                    loading={activeMutation.isPending && activeMutation.variables?.userId === record.user_id}
                    onChange={(checked) => activeMutation.mutate({ userId: record.user_id, isActive: checked })}
                />
            ),
        },
        {
            title: t('common:actions', 'Actions'),
            key: 'actions',
            render: (_, record) => (
                <Space>
                    <Button size="small" icon={<EditOutlined />} onClick={() => openEditModal(record)}>{t('ui.common.edit', 'Edit')}</Button>
                    <Button
                        size="small"
                        icon={<LinkOutlined />}
                        loading={inviteMutation.isPending}
                        onClick={() => inviteMutation.mutate({ userId: record.user_id })}
                    >
                        {t('sales_agents:invite', 'Invite')}
                    </Button>
                </Space>
            ),
        },
    ];

    return (
        <div>
            <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
                <Col>
                    <Title level={3} style={{ margin: 0 }}>{t('sales_agents:title', 'Sales agents')}</Title>
                </Col>
                <Col>
                    <Button type="primary" icon={<PlusOutlined />} onClick={openCreateModal}>
                        {t('sales_agents:add_sales_agent', 'Add sales agent')}
                    </Button>
                </Col>
            </Row>

            <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
                <Col xs={12} sm={6}>
                    <Card><Statistic title={t('sales_agents:total_agents', 'Total agents')} value={summary.total_agents || 0} prefix={<UserOutlined />} /></Card>
                </Col>
                <Col xs={12} sm={6}>
                    <Card><Statistic title={t('sales_agents:active_agents', 'Active agents')} value={summary.active_agents || 0} valueStyle={{ color: '#52c41a' }} /></Card>
                </Col>
                <Col xs={12} sm={6}>
                    <Card><Statistic title={t('sales_agents:agents.visits_today', 'Visits today')} value={summary.visits_today || 0} /></Card>
                </Col>
                <Col xs={12} sm={6}>
                    <Card><Statistic title={t('sales_agents:agents.orders_today', 'Orders today')} value={summary.orders_today || 0} /></Card>
                </Col>
            </Row>

            <Card style={{ marginBottom: 16 }}>
                <Row gutter={[16, 16]} align="middle">
                    <Col xs={24} sm={8}>
                        <Input
                            placeholder={t('sales_agents:search_agents_placeholder', 'Search by name or phone')}
                            prefix={<SearchOutlined />}
                            value={search}
                            onChange={(e) => { setSearch(e.target.value); setPage(1); }}
                            allowClear
                        />
                    </Col>
                    <Col xs={12} sm={5}>
                        <Select
                            placeholder={t('sales_agents:status', 'Status')}
                            value={statusFilter}
                            onChange={(val) => { setStatusFilter(val); setPage(1); }}
                            allowClear
                            style={{ width: '100%' }}
                        >
                            <Option value="active">{t('sales_agents:agent_active', 'Active')}</Option>
                            <Option value="inactive">{t('sales_agents:agent_inactive', 'Inactive')}</Option>
                        </Select>
                    </Col>
                    <Col>
                        <Button icon={<ReloadOutlined />} onClick={() => refetch()}>{t('ui.common.refresh', 'Refresh')}</Button>
                    </Col>
                </Row>
            </Card>

            <Card>
                <Table
                    columns={columns}
                    dataSource={items}
                    rowKey="user_id"
                    loading={isLoading}
                    pagination={{ current: page, pageSize: perPage, total, onChange: setPage, showSizeChanger: false, showTotal: (n) => `${t('common:total', 'Total')}: ${n}` }}
                    scroll={{ x: 900 }}
                />
            </Card>

            <Drawer
                title={selectedAgent?.full_name || t('sales_agents:agent_details', 'Details')}
                open={drawerOpen}
                onClose={() => { setDrawerOpen(false); setSelectedAgent(null); }}
                width={520}
            >
                {selectedAgent ? (
                    <Descriptions column={1} bordered size="small">
                        <Descriptions.Item label={t('sales_agents:contact_name', 'Name')}>{selectedAgent.full_name}</Descriptions.Item>
                        <Descriptions.Item label={t('sales_agents:phone', 'Phone')}>{selectedAgent.phone}</Descriptions.Item>
                        <Descriptions.Item label={t('sales_agents:districts', 'Districts')}>{(selectedAgent.districts || []).join(', ') || '—'}</Descriptions.Item>
                        <Descriptions.Item label={t('sales_agents:weekly_target', 'Weekly new-outlet target')}>{selectedAgent.weekly_new_outlet_target ?? '—'}</Descriptions.Item>
                        <Descriptions.Item label={t('sales_agents:employment_type', 'Employment')}>{selectedAgent.employment_type || '—'}</Descriptions.Item>
                        <Descriptions.Item label={t('sales_agents:telegram_linked', 'Telegram linked')}>{selectedAgent.telegram_linked ? t('ui.common.yes', 'Yes') : t('ui.common.no', 'No')}</Descriptions.Item>
                        <Descriptions.Item label={t('sales_agents:notes', 'Notes')}>{selectedAgent.notes || '—'}</Descriptions.Item>
                    </Descriptions>
                ) : null}
            </Drawer>

            <Modal
                title={editingAgent ? t('sales_agents:edit_sales_agent', 'Edit sales agent') : t('sales_agents:add_sales_agent', 'Add sales agent')}
                open={editorOpen}
                onCancel={() => { setEditorOpen(false); setEditingAgent(null); form.resetFields(); }}
                footer={null}
                destroyOnClose
            >
                <Form form={form} layout="vertical" onFinish={handleEditorSubmit}>
                    <Form.Item name="full_name" label={t('sales_agents:contact_name', 'Name')} rules={[{ required: true }]}><Input /></Form.Item>
                    <Form.Item name="phone" label={t('sales_agents:phone', 'Phone')} rules={[{ required: true }]}><Input /></Form.Item>
                    <Form.Item name="email" label={t('sales_agents:email', 'Email')}><Input /></Form.Item>
                    <Form.Item name="districts" label={t('sales_agents:districts', 'Districts')}>
                        <Select mode="multiple" allowClear options={districts.map((d) => ({ value: d.key, label: d.name }))} />
                    </Form.Item>
                    <Row gutter={12}>
                        <Col span={12}>
                            <Form.Item name="weekly_new_outlet_target" label={t('sales_agents:weekly_target', 'Weekly new-outlet target')}>
                                <InputNumber min={0} style={{ width: '100%' }} />
                            </Form.Item>
                        </Col>
                        <Col span={12}>
                            <Form.Item name="employment_type" label={t('sales_agents:employment_type', 'Employment')}>
                                <Select options={EMPLOYMENT_TYPES.map((v) => ({ value: v, label: t(`sales_agents:employment_${v}`, v) }))} />
                            </Form.Item>
                        </Col>
                    </Row>
                    <Form.Item name="notes" label={t('sales_agents:notes', 'Notes')}><Input.TextArea rows={2} /></Form.Item>
                    <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
                        <Button onClick={() => setEditorOpen(false)}>{t('ui.common.cancel', 'Cancel')}</Button>
                        <Button type="primary" htmlType="submit" loading={saveMutation.isPending}>{t('ui.common.save', 'Save')}</Button>
                    </Space>
                </Form>
            </Modal>

            <Modal
                title={t('sales_agents:invite_link', 'Invite link')}
                open={inviteModalOpen}
                onCancel={() => setInviteModalOpen(false)}
                footer={[
                    <Button key="copy" type="primary" icon={<CopyOutlined />} onClick={handleCopyInvite}>{t('sales_agents:copy_link', 'Copy link')}</Button>,
                    <Button key="close" onClick={() => setInviteModalOpen(false)}>{t('ui.common.close', 'Close')}</Button>,
                ]}
            >
                <Space direction="vertical" style={{ width: '100%' }}>
                    <Text>{t('sales_agents:share_invite_description', 'Send this one-time link to the agent; it opens the staff bot and binds their Telegram account.')}</Text>
                    <Input value={inviteLink} readOnly />
                </Space>
            </Modal>
        </div>
    );
};

export default SalesAgents;
