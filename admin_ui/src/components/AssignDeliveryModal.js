import React, { useState } from 'react';
import {
    Modal, Select, Space, Typography, Tag, Spin, Row, Col, Statistic,
    message, Alert,
} from 'antd';
import { CarOutlined, UserOutlined } from '@ant-design/icons';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import staffService from '../services/staffService';

const { Text } = Typography;
const { Option } = Select;

/**
 * Modal to assign or reassign a delivery to a delivery person.
 *
 * Props:
 *   open {boolean} - whether the modal is visible
 *   onCancel {function} - close handler
 *   deliveryId {number} - target delivery ID
 *   currentPersonId {number|null} - currently assigned person (null = unassigned)
 *   onSuccess {function} - called after successful assignment
 */
const AssignDeliveryModal = ({ open, onCancel, deliveryId, currentPersonId, onSuccess }) => {
    const { t } = useTranslation(['staff', 'common']);
    const queryClient = useQueryClient();
    const [selectedPersonId, setSelectedPersonId] = useState(null);

    // Fetch available delivery persons
    const { data: personsData, isLoading: loadingPersons } = useQuery({
        queryKey: ['availableDeliveryPersons'],
        queryFn: () => staffService.getDeliveryPersons({ status: 'active', per_page: 100 }),
        enabled: open,
    });

    const persons = personsData?.data?.data?.items || [];

    const isReassign = Boolean(currentPersonId);

    const assignMutation = useMutation({
        mutationFn: () =>
            isReassign
                ? staffService.reassignDelivery(deliveryId, selectedPersonId)
                : staffService.assignDelivery(deliveryId, selectedPersonId),

        onSuccess: () => {
            message.success(
                isReassign ? t('staff:delivery_reassigned') : t('staff:delivery_assigned')
            );
            queryClient.invalidateQueries({
                queryKey: ['staffDeliveryPersons'],
            });
            setSelectedPersonId(null);
            onSuccess?.();
            onCancel();
        },

        onError: (err) => {
            const msg = err?.response?.data?.message || t('common:error_occurred');
            message.error(msg);
        },
    });

    const selectedPerson = persons.find((p) => p.user_id === selectedPersonId);

    return (
        <Modal
            title={isReassign ? t('staff:reassign_delivery') : t('staff:assign_delivery')}
            open={open}
            onCancel={() => { setSelectedPersonId(null); onCancel(); }}
            onOk={() => assignMutation.mutate()}
            okText={isReassign ? t('staff:reassign') : t('staff:assign')}
            cancelText={t('common:cancel')}
            confirmLoading={assignMutation.isPending}
            okButtonProps={{ disabled: !selectedPersonId }}
        >
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                {isReassign && (
                    <Alert
                        type="info"
                        message={t('staff:reassign_info')}
                        showIcon
                    />
                )}

                <div>
                    <Text strong>{t('staff:select_delivery_person')}</Text>
                    <Select
                        placeholder={t('staff:select_person_placeholder')}
                        style={{ width: '100%', marginTop: 8 }}
                        value={selectedPersonId}
                        onChange={setSelectedPersonId}
                        loading={loadingPersons}
                        showSearch
                        optionFilterProp="children"
                        notFoundContent={loadingPersons ? <Spin size="small" /> : null}
                    >
                        {persons.map((person) => (
                            <Option
                                key={person.user_id}
                                value={person.user_id}
                                disabled={person.user_id === currentPersonId}
                            >
                                <Space>
                                    <UserOutlined />
                                    {person.full_name}
                                    <Tag color={person.is_available ? 'green' : 'orange'}>
                                        {person.is_available ? t('staff:available') : t('staff:busy')}
                                    </Tag>
                                    <Text type="secondary">
                                        {person.current_active_deliveries || 0}/{person.max_concurrent_deliveries || 3}
                                    </Text>
                                </Space>
                            </Option>
                        ))}
                    </Select>
                </div>

                {selectedPerson && (
                    <Row gutter={[16, 8]}>
                        <Col span={8}>
                            <Statistic
                                title={t('staff:active_now')}
                                value={selectedPerson.current_active_deliveries || 0}
                                valueStyle={{ fontSize: 18 }}
                            />
                        </Col>
                        <Col span={8}>
                            <Statistic
                                title={t('staff:total_deliveries')}
                                value={selectedPerson.total_deliveries || 0}
                                valueStyle={{ fontSize: 18 }}
                            />
                        </Col>
                        <Col span={8}>
                            <Statistic
                                title={t('staff:rating')}
                                value={selectedPerson.average_rating || 0}
                                precision={1}
                                valueStyle={{ fontSize: 18 }}
                            />
                        </Col>
                    </Row>
                )}
            </Space>
        </Modal>
    );
};

export default AssignDeliveryModal;
