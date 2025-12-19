import React, { useState } from 'react';
import { Form, Input, Button, Card, Typography, Space, Divider, Alert } from 'antd';
import { UserOutlined, LockOutlined, SettingOutlined } from '@ant-design/icons';
import { useNavigate, useLocation } from 'react-router-dom';
import { useAuthStore } from '../stores/authStore';
import { useTranslation } from 'react-i18next';

const { Title, Text } = Typography;

const Login = () => {
  const { t } = useTranslation();
  const [form] = Form.useForm();
  const navigate = useNavigate();
  const location = useLocation();
  const { login, isLoading } = useAuthStore();

  const from = location.state?.from?.pathname || '/dashboard';

  const handleSubmit = async (values) => {
    const result = await login(values);
    if (result.success) {
      navigate(from, { replace: true });
    }
  };

  return (
    <div style={{
      display: 'flex',
      justifyContent: 'center',
      alignItems: 'center',
      minHeight: '100vh',
      background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
      padding: '20px'
    }}>
      <Card
        style={{
          width: '100%',
          maxWidth: 400,
          boxShadow: '0 10px 25px rgba(0,0,0,0.1)',
          borderRadius: 10
        }}
      >
        <div style={{ textAlign: 'center', marginBottom: 24 }}>
          <Title level={2} style={{ color: '#1890ff', marginBottom: 8 }}>
            {t('ui.login.app_name')}
          </Title>
          <Text type="secondary">{t('ui.login.subtitle')}</Text>
        </div>

        <Alert
          message={t('ui.login.restricted_access')}
          description={t('ui.login.restricted_description')}
          type="warning"
          icon={<SettingOutlined />}
          showIcon
          style={{ marginBottom: 24 }}
        />

        <Divider />

        <Form
          form={form}
          name="login"
          onFinish={handleSubmit}
          layout="vertical"
          requiredMark={false}
        >
          <Form.Item
            name="email"
            label={t('ui.login.email_or_phone')}
            rules={[
              { required: true, message: t('ui.login.email_required') }
            ]}
          >
            <Input
              prefix={<UserOutlined />}
              placeholder={t('ui.login.email_or_phone_placeholder')}
              size="large"
            />
          </Form.Item>

          <Form.Item
            name="password"
            label={t('ui.login.password')}
            rules={[{ required: true, message: t('ui.login.password_required') }]}
          >
            <Input.Password
              prefix={<LockOutlined />}
              placeholder={t('ui.login.password_placeholder')}
              size="large"
            />
          </Form.Item>

          <Form.Item>
            <Button
              type="primary"
              htmlType="submit"
              size="large"
              loading={isLoading}
              block
              style={{ marginTop: 8 }}
            >
              {t('ui.login.sign_in')}
            </Button>
          </Form.Item>
        </Form>

        <div style={{ textAlign: 'center', marginTop: 16 }}>
          <Text type="secondary" style={{ fontSize: '12px' }}>
            {t('ui.login.copyright')}
          </Text>
        </div>
      </Card>
    </div>
  );
};

export default Login;