import React, { useState } from 'react';
import { Button, Form, Input, Modal, Popconfirm, Select, Space, Table, Tag, message } from 'antd';
import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import salesService from '../../services/salesService';
import { extractApiErrorMessage } from '../../utils/apiError';
import { CONTACT_ROLES } from './outletVocabulary';

// D30: the admin's hands on an outlet's contacts. The primary contact's phone is what approval
// matches accounts on, so a wrong number has to be fixable here. The one-primary rule is the
// backend's: "Make primary" sends `is_primary: true` and the service moves the flag.
const OutletContactsTab = ({ outlet, onChanged }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const [editing, setEditing] = useState(null); // null = closed, {} = a new contact, a row = edit it
  const onError = (err) => message.error(extractApiErrorMessage(err, t('ui.common.error_occurred', 'An error occurred')));
  const done = (text) => { message.success(text); setEditing(null); onChanged(); };
  const saved = () => done(t('sales_agents:contacts.saved', 'Contact saved'));

  const saveMutation = useMutation({
    mutationFn: ({ id, payload }) => (id
      ? salesService.updateOutletContact(outlet.id, id, payload)
      : salesService.addOutletContact(outlet.id, payload)),
    onSuccess: saved,
    onError,
  });
  const primaryMutation = useMutation({
    mutationFn: (id) => salesService.updateOutletContact(outlet.id, id, { is_primary: true }),
    onSuccess: saved,
    onError,
  });
  const deleteMutation = useMutation({
    mutationFn: (id) => salesService.deleteOutletContact(outlet.id, id),
    onSuccess: () => done(t('sales_agents:contacts.deleted', 'Contact deleted')),
    onError,
  });

  const roleLabel = (role) => t(`sales_agents:contacts.role.${role}`, role);
  // Explicit nulls for emptied fields: the PUT is exclude_unset, so an omitted key is ignored.
  const submit = (values) => saveMutation.mutate({
    id: editing?.id,
    payload: {
      name: values.name?.trim() || null,
      phone: values.phone?.trim() || null,
      role: values.role,
      presence_window: values.presence_window?.trim() || null,
    },
  });

  const columns = [
    {
      title: t('sales_agents:contact_name', 'Name'),
      dataIndex: 'name',
      render: (value, record) => (
        <Space>{value}{record.is_primary && <Tag color="blue">{t('sales_agents:contacts.primary', 'Primary')}</Tag>}</Space>
      ),
    },
    { title: t('sales_agents:phone', 'Phone'), dataIndex: 'phone', render: (v) => v || '—' },
    { title: t('sales_agents:role', 'Role'), dataIndex: 'role', render: roleLabel },
    { title: t('sales_agents:contacts.presence_window', 'When available'), dataIndex: 'presence_window', render: (v) => v || '—' },
    {
      key: 'actions',
      render: (_, record) => (
        <Space>
          <Button size="small" onClick={() => setEditing(record)}>{t('sales_agents:contacts.edit', 'Edit')}</Button>
          {!record.is_primary && (
            <Button size="small" onClick={() => primaryMutation.mutate(record.id)}>{t('sales_agents:contacts.make_primary', 'Make primary')}</Button>
          )}
          <Popconfirm title={t('sales_agents:contacts.delete_confirm', 'Delete this contact?')} onConfirm={() => deleteMutation.mutate(record.id)}>
            <Button size="small" danger>{t('sales_agents:contacts.delete', 'Delete')}</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Button type="primary" onClick={() => setEditing({})}>{t('sales_agents:contacts.add', 'Add contact')}</Button>
      <Table rowKey="id" pagination={false} dataSource={outlet.contacts || []} columns={columns} />
      <Modal
        title={editing?.id ? t('sales_agents:contacts.edit_title', 'Edit contact') : t('sales_agents:contacts.add', 'Add contact')}
        open={editing !== null}
        onCancel={() => setEditing(null)}
        footer={null}
        destroyOnClose
      >
        <Form
          layout="vertical"
          initialValues={{ name: editing?.name, phone: editing?.phone, role: editing?.role || 'owner', presence_window: editing?.presence_window }}
          onFinish={submit}
        >
          <Form.Item name="name" label={t('sales_agents:contact_name', 'Name')}><Input maxLength={100} /></Form.Item>
          <Form.Item name="phone" label={t('sales_agents:phone', 'Phone')}><Input maxLength={20} placeholder="+998901234567" /></Form.Item>
          <Form.Item name="role" label={t('sales_agents:role', 'Role')}>
            <Select options={CONTACT_ROLES.map((role) => ({ value: role, label: roleLabel(role) }))} data-testid="contact-role-select" />
          </Form.Item>
          <Form.Item name="presence_window" label={t('sales_agents:contacts.presence_window', 'When available')}><Input maxLength={100} /></Form.Item>
          <Button type="primary" htmlType="submit" loading={saveMutation.isPending}>{t('ui.common.save', 'Save')}</Button>
        </Form>
      </Modal>
    </Space>
  );
};

export default OutletContactsTab;
