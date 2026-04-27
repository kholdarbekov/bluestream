import React, { useState } from 'react';
import { Dropdown, Button, Space, message } from 'antd';
import { GlobalOutlined, SyncOutlined, CheckOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { reloadTranslations } from '../../i18n';

const LanguageSwitcher = ({ showSyncButton = true, size = 'middle' }) => {
  const { i18n, t } = useTranslation();
  const [syncing, setSyncing] = useState(false);

  // Language options with flags
  const languages = [
    { code: 'uz', name: "O'zbek", flag: '🇺🇿' },
    { code: 'en', name: 'English', flag: '🇬🇧' },
    { code: 'ru', name: 'Русский', flag: '🇷🇺' }
  ];

  // Get current language details
  const currentLang = languages.find(lang => lang.code === i18n.language) || languages[0];

  // Handle language change
  const handleLanguageChange = async (languageCode) => {
    try {
      await i18n.changeLanguage(languageCode);
      message.success(`${t('ui.language')  } changed successfully`);
    } catch (error) {
      console.error('Failed to change language:', error);
      message.error('Failed to change language');
    }
  };

  // Handle translation sync from database
  const handleSync = async () => {
    setSyncing(true);
    try {
      await reloadTranslations();
      message.success(t('ui.sync_success'));
    } catch (error) {
      console.error('Failed to sync translations:', error);
      message.error(t('ui.sync_failed'));
    } finally {
      setSyncing(false);
    }
  };

  // Dropdown menu items
  const menuItems = languages.map(lang => ({
    key: lang.code,
    label: (
      <Space>
        <span style={{ fontSize: '16px' }}>{lang.flag}</span>
        <span>{lang.name}</span>
        {lang.code === i18n.language && <CheckOutlined style={{ color: '#52c41a' }} />}
      </Space>
    ),
    onClick: () => handleLanguageChange(lang.code)
  }));

  return (
    <Space size={8}>
      {/* Language Dropdown */}
      <Dropdown
        menu={{ items: menuItems }}
        placement="bottomRight"
        trigger={['click']}
      >
        <Button
          type="text"
          icon={<GlobalOutlined />}
          size={size}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '4px'
          }}
        >
          <span style={{ fontSize: '16px' }}>{currentLang.flag}</span>
        </Button>
      </Dropdown>

      {/* Sync Button - Only show if enabled */}
      {showSyncButton && (
        <Button
          type="text"
          icon={<SyncOutlined spin={syncing} />}
          onClick={handleSync}
          loading={syncing}
          size={size}
          title={t('ui.sync_translations')}
          style={{
            display: 'flex',
            alignItems: 'center'
          }}
        />
      )}
    </Space>
  );
};

export default LanguageSwitcher;
