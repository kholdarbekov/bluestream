import React from 'react';
import { Card, Typography, Button } from 'antd';
import { SettingOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';

const { Title, Paragraph } = Typography;

const Settings = () => {
  // Load settings namespace for ui.settings.* keys
  const { t } = useTranslation('settings');

  return (
    <Card>
      <div style={{ textAlign: 'center', padding: '40px 0' }}>
        <SettingOutlined style={{ fontSize: 64, color: '#1890ff', marginBottom: 16 }} />
        <Title level={2}>{t('ui.settings.title')}</Title>
        <Paragraph>
          {t('ui.settings.description')}
        </Paragraph>
        <Button type="primary" size="large">
          {t('ui.settings.coming_soon')}
        </Button>
      </div>
    </Card>
  );
};

export default Settings;
