import React from 'react';
import { useTranslation } from 'react-i18next';
import { Tooltip } from 'antd';
import { WarningOutlined } from '@ant-design/icons';

/**
 * MissingTranslationWrapper
 *
 * Wraps text content and highlights missing translations in development mode
 * with a visual indicator and tooltip.
 *
 * Usage:
 * import { Trans } from 'react-i18next';
 * <Trans components={[<MissingTranslationWrapper />]}>ui.some.key</Trans>
 */
const MissingTranslationWrapper = ({ children }) => {
  const { i18n } = useTranslation();

  // Only show highlighting in development mode
  if (process.env.NODE_ENV !== 'development') {
    return <>{children}</>;
  }

  // Check if this looks like a missing translation (starts with ⚠️)
  const text = typeof children === 'string' ? children : '';
  const isMissing = text.startsWith('⚠️');

  if (!isMissing) {
    return <>{children}</>;
  }

  // Extract the key (remove the warning emoji)
  const key = text.replace('⚠️ ', '');

  return (
    <Tooltip
      title={`Missing translation: ${key} (${i18n.language})`}
      color="red"
    >
      <span
        style={{
          backgroundColor: '#fff3cd',
          border: '1px dashed #ffc107',
          padding: '2px 6px',
          borderRadius: '3px',
          color: '#856404',
          cursor: 'help',
          fontFamily: 'monospace',
          fontSize: '0.9em'
        }}
      >
        <WarningOutlined style={{ marginRight: 4, color: '#ff9800' }} />
        {key}
      </span>
    </Tooltip>
  );
};

export default MissingTranslationWrapper;
