import React from 'react';
import { Tag, Tooltip, Typography } from 'antd';
import { useTranslation } from 'react-i18next';

import { formatDateTime, toTashkent } from '../../utils/dateUtils';

const { Text } = Typography;

const ForwardedHeader = ({ message }) => {
  const { t } = useTranslation('common');
  if (!message.forwarded_origin_type) return null;

  // A hidden-user forward gives us a display name Telegram does not vouch for,
  // so the label must not present it as a verified identity.
  const label = message.forwarded_origin_type === 'hidden_user'
    ? t('ui.support.forwarded_from_hidden', { defaultValue: 'Forwarded from a hidden sender' })
    : t('ui.support.forwarded_from', {
      name: message.forwarded_from,
      defaultValue: `Forwarded from ${message.forwarded_from}`,
    });

  return (
    <div style={{ fontSize: 12, opacity: 0.7, marginBottom: 4, fontStyle: 'italic' }}>
      ↪ {label}
    </div>
  );
};

const MessageBubble = ({ message, children }) => {
  const { t } = useTranslation('common');
  const outbound = message.direction === 'outbound';

  return (
    <div style={{ textAlign: outbound ? 'right' : 'left', margin: '6px 0' }}>
      <div
        style={{
          display: 'inline-block',
          maxWidth: '75%',
          padding: '6px 10px',
          borderRadius: 8,
          background: outbound ? '#d6e4ff' : '#fff',
          border: '1px solid #eee',
          textAlign: 'left',
          whiteSpace: 'pre-wrap',
        }}
      >
        <ForwardedHeader message={message} />
        {children}
        {message.content && <div>{message.content}</div>}
        <div style={{ marginTop: 4, textAlign: 'right' }}>
          <Tooltip title={formatDateTime(message.created_at, 'DD/MM/YYYY, HH:mm')}>
            <Text type="secondary" style={{ fontSize: 11 }}>
              {toTashkent(message.created_at).format('HH:mm')}
            </Text>
          </Tooltip>
        </div>
        {outbound && message.delivery_status === 'failed' && (
          <Tag color="red" style={{ marginTop: 4 }}>
            {t('ui.support.delivery_failed', { defaultValue: 'Not delivered' })}
          </Tag>
        )}
      </div>
    </div>
  );
};

export default MessageBubble;
