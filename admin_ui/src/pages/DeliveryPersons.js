import React, { useState } from 'react';
import {
    Card, Table, Tag, Space, Button, Input, Select, Row, Col,
    Statistic, Switch, Drawer, Descriptions, Typography, Tooltip, message, Rate,
    Modal, Form, InputNumber,
} from 'antd';
import {
    SearchOutlined, UserOutlined, CarOutlined, PhoneOutlined,
    BellOutlined, EnvironmentOutlined, ReloadOutlined, PlusOutlined, EditOutlined, LinkOutlined, CopyOutlined,
} from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient } from 'react-query';
import { useTranslation } from 'react-i18next';
import staffService from '../services/staffService';
import DeliveryMap from '../components/DeliveryMap';

const { Title, Text } = Typography;
const { Option } = Select;

const DeliveryPersons = () => {
    const { t } = useTranslation(['staff', 'common']);
    const queryClient = useQueryClient();
    const [form] = Form.useForm();

    // List/filter state
    const [page, setPage] = useState(1);
    const [perPage] = useState(20);
    const [search, setSearch] = useState('');
    const [statusFilter, setStatusFilter] = useState(undefined);
    const [availableFilter, setAvailableFilter] = useState(undefined);
    const [showMap, setShowMap] = useState(false);

    // Detail/create/edit state
    const [selectedPerson, setSelectedPerson] = useState(null);
    const [drawerOpen, setDrawerOpen] = useState(false);
    const [editorOpen, setEditorOpen] = useState(false);
    const [editingPerson, setEditingPerson] = useState(null);

    // Invite link state
    const [inviteModalOpen, setInviteModalOpen] = useState(false);
    const [inviteLink, setInviteLink] = useState('');

    const { data, isLoading, refetch } = useQuery(
        ['staffDeliveryPersons', page, perPage, search, statusFilter, availableFilter],
        () =>
            staffService.getDeliveryPersons({
                page,
                per_page: perPage,
                search: search || undefined,
                status: statusFilter,
                available: availableFilter,
            }),
        { keepPreviousData: true }
    );

    const { data: detailData, isLoading: detailLoading } = useQuery(
        ['staffDeliveryPerson', selectedPerson?.id],
        () => staffService.getDeliveryPerson(selectedPerson.id),
        { enabled: !!selectedPerson?.id }
    );

    const muteMutation = useMutation(
        ({ id, muted }) => staffService.muteNotifications(id, muted),
        {
            onSuccess: () => {
                message.success(t('staff:notifications_updated'));
                queryClient.invalidateQueries('staffDeliveryPersons');
            },
            onError: () => message.error(t('common:error_occurred')),
        }
    );

    const saveMutation = useMutation(
        (payload) => {
            if (editingPerson?.id) {
                return staffService.updateDeliveryPerson(editingPerson.id, payload);
            }
            return staffService.createDeliveryPerson(payload);
        },
        {
            onSuccess: () => {
                message.success(
                    editingPerson
                        ? t('staff:delivery_person_updated')
                        : t('staff:delivery_person_created')
                );
                setEditorOpen(false);
                setEditingPerson(null);
                form.resetFields();
                queryClient.invalidateQueries('staffDeliveryPersons');
            },
            onError: (err) => {
                const backendMessage = err?.response?.data?.message;
                message.error(backendMessage || t('common:error_occurred'));
            },
        }
    );

    const inviteMutation = useMutation(
        ({ userId }) => staffService.generateInviteLink({ user_id: userId, role: 'delivery_driver' }),
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
    const detail = detailData?.data?.data?.delivery_person;

    const openCreateModal = () => {
        setEditingPerson(null);
        form.setFieldsValue({
            is_active: true,
            is_available: true,
            max_concurrent_deliveries: 3,
            working_hours_start: '09:00',
            working_hours_end: '18:00',
        });
        setEditorOpen(true);
    };

    const openEditModal = (record) => {
        setEditingPerson(record);
        form.setFieldsValue({
            full_name: record.full_name,
            phone: record.phone,
            email: record.email,
            vehicle_type: record.vehicle_type,
            vehicle_number: record.vehicle_number,
            max_concurrent_deliveries: record.max_concurrent_deliveries || 3,
            working_hours_start: record.working_hours_start || '09:00',
            working_hours_end: record.working_hours_end || '18:00',
            is_active: !!record.is_active,
            is_available: !!record.is_available,
        });
        setEditorOpen(true);
    };

    const handleEditorSubmit = (values) => {
        const payload = {
            ...values,
            phone: values.phone?.trim(),
            full_name: values.full_name?.trim(),
            email: values.email?.trim() || null,
            vehicle_type: values.vehicle_type || null,
            vehicle_number: values.vehicle_number?.trim() || null,
        };
        saveMutation.mutate(payload);
    };

    const handleGenerateInvite = (record) => {
        if (!record?.user_id) {
            message.error(t('common:error_occurred'));
            return;
        }
        inviteMutation.mutate({ userId: record.user_id });
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
                    <a onClick={() => { setSelectedPerson(record); setDrawerOpen(true); }}>
                        {text}
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
                    {text}
                </Space>
            ),
        },
        {
            title: t('staff:status'),
            key: 'status',
            render: (_, record) => (
                <Space>
                    <Tag color={record.is_active ? 'green' : 'red'}>
                        {record.is_active ? t('staff:active') : t('staff:inactive')}
                    </Tag>
                    {record.is_active && (
                        <Tag color={record.is_available ? 'blue' : 'orange'}>
                            {record.is_available ? t('staff:available') : t('staff:busy')}
                        </Tag>
                    )}
                </Space>
            ),
        },
        {
            title: t('staff:vehicle'),
            dataIndex: 'vehicle_type',
            key: 'vehicle_type',
            render: (text, record) =>
                text ? (
                    <Space>
                        <CarOutlined />
                        {text} {record.vehicle_number ? `(${record.vehicle_number})` : ''}
                    </Space>
                ) : (
                    <Text type="secondary">—</Text>
                ),
        },
        {
            title: t('staff:deliveries'),
            key: 'deliveries',
            render: (_, record) => (
                <Space direction="vertical" size={0}>
                    <Text>{record.total_deliveries || 0} {t('staff:total')}</Text>
                    <Text type="secondary">
                        {record.current_active_deliveries || 0} {t('staff:active_now')}
                    </Text>
                </Space>
            ),
        },
        {
            title: t('staff:rating'),
            dataIndex: 'average_rating',
            key: 'average_rating',
            render: (val) =>
                val > 0 ? <Rate disabled defaultValue={val} allowHalf style={{ fontSize: 14 }} /> : <Text type="secondary">—</Text>,
        },
        {
            title: t('staff:notifications'),
            key: 'notifications',
            render: (_, record) => (
                <Tooltip title={record.notifications_muted ? t('staff:unmute') : t('staff:mute')}>
                    <Switch
                        checked={!record.notifications_muted}
                        onChange={(checked) =>
                            muteMutation.mutate({ id: record.id, muted: !checked })
                        }
                        checkedChildren={<BellOutlined />}
                        unCheckedChildren={<BellOutlined />}
                        loading={muteMutation.isLoading}
                    />
                </Tooltip>
            ),
        },
        {
            title: t('staff:actions'),
            key: 'actions',
            render: (_, record) => (
                <Space>
                    <Button
                        size="small"
                        onClick={() => {
                            setSelectedPerson(record);
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
                    {record.current_location_lat && (
                        <Tooltip title={t('staff:view_on_map')}>
                            <Button size="small" icon={<EnvironmentOutlined />} />
                        </Tooltip>
                    )}
                </Space>
            ),
        },
    ];

    return (
        <div>
            <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
                <Col>
                    <Title level={3} style={{ margin: 0 }}>{t('staff:delivery_persons')}</Title>
                </Col>
                <Col>
                    <Button type="primary" icon={<PlusOutlined />} onClick={openCreateModal}>
                        {t('staff:add_delivery_person')}
                    </Button>
                </Col>
            </Row>

            <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
                <Col xs={12} sm={6}>
                    <Card>
                        <Statistic
                            title={t('staff:total_drivers')}
                            value={summary.total_drivers || 0}
                            prefix={<UserOutlined />}
                        />
                    </Card>
                </Col>
                <Col xs={12} sm={6}>
                    <Card>
                        <Statistic
                            title={t('staff:active_drivers')}
                            value={summary.active_drivers || 0}
                            valueStyle={{ color: '#52c41a' }}
                        />
                    </Card>
                </Col>
                <Col xs={12} sm={6}>
                    <Card>
                        <Statistic
                            title={t('staff:available_now')}
                            value={summary.available_drivers || 0}
                            valueStyle={{ color: '#1890ff' }}
                        />
                    </Card>
                </Col>
                <Col xs={12} sm={6}>
                    <Card>
                        <Statistic
                            title={t('staff:deliveries_today')}
                            value={summary.deliveries_today || 0}
                        />
                    </Card>
                </Col>
            </Row>

            <div style={{ marginBottom: 16 }}>
                <Button
                    type={showMap ? 'primary' : 'default'}
                    icon={<EnvironmentOutlined />}
                    onClick={() => setShowMap(!showMap)}
                >
                    {showMap ? t('staff:hide_map') : t('staff:show_map')}
                </Button>
            </div>
            {showMap && (
                <div style={{ marginBottom: 16 }}>
                    <DeliveryMap height={400} />
                </div>
            )}

            <Card style={{ marginBottom: 16 }}>
                <Row gutter={[16, 16]} align="middle">
                    <Col xs={24} sm={8}>
                        <Input
                            placeholder={t('staff:search_placeholder')}
                            prefix={<SearchOutlined />}
                            value={search}
                            onChange={(e) => { setSearch(e.target.value); setPage(1); }}
                            allowClear
                        />
                    </Col>
                    <Col xs={12} sm={5}>
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
                    <Col xs={12} sm={5}>
                        <Select
                            placeholder={t('staff:availability')}
                            value={availableFilter}
                            onChange={(val) => { setAvailableFilter(val); setPage(1); }}
                            allowClear
                            style={{ width: '100%' }}
                        >
                            <Option value="true">{t('staff:available')}</Option>
                            <Option value="false">{t('staff:busy')}</Option>
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
                    scroll={{ x: 1024 }}
                />
            </Card>

            <Drawer
                title={detail?.full_name || selectedPerson?.full_name || t('staff:details')}
                open={drawerOpen}
                onClose={() => { setDrawerOpen(false); setSelectedPerson(null); }}
                width={520}
            >
                {detailLoading ? (
                    <div style={{ textAlign: 'center', padding: 40 }}>{t('common:loading')}</div>
                ) : detail ? (
                    <Space direction="vertical" size="large" style={{ width: '100%' }}>
                        <Descriptions column={1} bordered size="small">
                            <Descriptions.Item label={t('staff:name')}>{detail.full_name}</Descriptions.Item>
                            <Descriptions.Item label={t('staff:phone')}>{detail.phone}</Descriptions.Item>
                            <Descriptions.Item label={t('staff:email')}>{detail.email || '—'}</Descriptions.Item>
                            <Descriptions.Item label={t('staff:employee_id')}>{detail.employee_id || '—'}</Descriptions.Item>
                            <Descriptions.Item label={t('staff:vehicle')}>
                                {detail.vehicle_type ? `${detail.vehicle_type} (${detail.vehicle_number || ''})` : '—'}
                            </Descriptions.Item>
                            <Descriptions.Item label={t('staff:working_hours')}>
                                {detail.working_hours_start} - {detail.working_hours_end}
                            </Descriptions.Item>
                            <Descriptions.Item label={t('staff:hire_date')}>{detail.hire_date || '—'}</Descriptions.Item>
                            <Descriptions.Item label={t('staff:max_concurrent')}>
                                {detail.max_concurrent_deliveries}
                            </Descriptions.Item>
                        </Descriptions>

                        {detail.stats && (
                            <Card title={t('staff:performance_stats')} size="small">
                                <Row gutter={[16, 16]}>
                                    <Col span={12}>
                                        <Statistic title={t('staff:total_deliveries')} value={detail.stats.total_deliveries} />
                                    </Col>
                                    <Col span={12}>
                                        <Statistic
                                            title={t('staff:success_rate')}
                                            value={detail.stats.success_rate}
                                            suffix="%"
                                            valueStyle={{ color: detail.stats.success_rate >= 90 ? '#52c41a' : '#faad14' }}
                                        />
                                    </Col>
                                    <Col span={12}>
                                        <Statistic title={t('staff:cash_collected')} value={detail.stats.total_cash_collected} prefix="UZS" />
                                    </Col>
                                    <Col span={12}>
                                        <Statistic
                                            title={t('staff:avg_delivery_time')}
                                            value={detail.stats.avg_delivery_time_minutes || '—'}
                                            suffix={detail.stats.avg_delivery_time_minutes ? t('staff:minutes') : ''}
                                        />
                                    </Col>
                                </Row>
                            </Card>
                        )}

                        {detail.emergency_contact_name && (
                            <Descriptions column={1} bordered size="small" title={t('staff:emergency_contact')}>
                                <Descriptions.Item label={t('staff:name')}>{detail.emergency_contact_name}</Descriptions.Item>
                                <Descriptions.Item label={t('staff:phone')}>{detail.emergency_contact_phone}</Descriptions.Item>
                            </Descriptions>
                        )}

                        <Space>
                            <Button icon={<EditOutlined />} onClick={() => openEditModal(detail)}>
                                {t('common:edit')}
                            </Button>
                            <Button icon={<LinkOutlined />} onClick={() => handleGenerateInvite(detail)}>
                                {t('staff:invite')}
                            </Button>
                        </Space>
                    </Space>
                ) : null}
            </Drawer>

            <Modal
                title={editingPerson ? t('staff:edit_delivery_person') : t('staff:add_delivery_person')}
                open={editorOpen}
                onCancel={() => {
                    setEditorOpen(false);
                    setEditingPerson(null);
                    form.resetFields();
                }}
                footer={null}
                destroyOnClose
            >
                <Form form={form} layout="vertical" onFinish={handleEditorSubmit}>
                    <Form.Item
                        name="full_name"
                        label={t('staff:name')}
                        rules={[{ required: true, message: t('staff:name') }]}
                    >
                        <Input />
                    </Form.Item>
                    <Form.Item
                        name="phone"
                        label={t('staff:phone')}
                        rules={[{ required: true, message: t('staff:phone') }]}
                    >
                        <Input />
                    </Form.Item>
                    <Form.Item name="email" label={t('staff:email')}>
                        <Input />
                    </Form.Item>
                    <Form.Item name="vehicle_type" label={t('staff:vehicle')}>
                        <Select allowClear>
                            <Option value="motorcycle">{t('staff:motorcycle')}</Option>
                            <Option value="car">{t('staff:car')}</Option>
                            <Option value="truck">{t('staff:truck')}</Option>
                            <Option value="bicycle">{t('staff:bicycle')}</Option>
                        </Select>
                    </Form.Item>
                    <Form.Item name="vehicle_number" label={t('staff:vehicle_number')}>
                        <Input />
                    </Form.Item>
                    <Form.Item name="max_concurrent_deliveries" label={t('staff:max_concurrent')}>
                        <InputNumber min={1} max={20} style={{ width: '100%' }} />
                    </Form.Item>
                    <Row gutter={12}>
                        <Col span={12}>
                            <Form.Item name="working_hours_start" label={t('staff:working_hours_start')}>
                                <Input placeholder="09:00" />
                            </Form.Item>
                        </Col>
                        <Col span={12}>
                            <Form.Item name="working_hours_end" label={t('staff:working_hours_end')}>
                                <Input placeholder="18:00" />
                            </Form.Item>
                        </Col>
                    </Row>
                    <Row gutter={12}>
                        <Col span={12}>
                            <Form.Item name="is_active" label={t('staff:active')} valuePropName="checked">
                                <Switch />
                            </Form.Item>
                        </Col>
                        <Col span={12}>
                            <Form.Item name="is_available" label={t('staff:available')} valuePropName="checked">
                                <Switch />
                            </Form.Item>
                        </Col>
                    </Row>
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

export default DeliveryPersons;
