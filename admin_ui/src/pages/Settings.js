import React from 'react';
import { Card, Typography, Button } from 'antd';
import { SettingOutlined } from '@ant-design/icons';

const { Title, Paragraph } = Typography;

const Settings = () => {
  return (
    <Card>
      <div style={{ textAlign: 'center', padding: '40px 0' }}>
        <SettingOutlined style={{ fontSize: 64, color: '#1890ff', marginBottom: 16 }} />
        <Title level={2}>System Settings</Title>
        <Paragraph>
          Configure system settings, manage integrations, backup options, and security preferences.
        </Paragraph>
        <Button type="primary" size="large">
          Coming Soon
        </Button>
      </div>
    </Card>
  );
};

export default Settings;