import React from 'react';
import { Badge, Empty, List, Space, Typography } from 'antd';
import { useTranslation } from 'react-i18next';

import { formatDateTimeShort, toTashkent } from '../../utils/dateUtils';

const { Text } = Typography;

const ConversationList = ({ conversations = [], activeId, onSelect }) => {
  const { t } = useTranslation('common');

  if (conversations.length === 0) {
    return <Empty description={t('ui.support.no_conversations', { defaultValue: 'No conversations yet' })} />;
  }

  const today = toTashkent(new Date()).format('YYYY-MM-DD');
  const stamp = (value) => {
    if (!value) return '';
    const at = toTashkent(value);
    return at.format('YYYY-MM-DD') === today ? at.format('HH:mm') : formatDateTimeShort(value);
  };

  return (
    <List
      dataSource={conversations}
      renderItem={(c) => (
        <List.Item
          onClick={() => onSelect(c.id)}
          style={{ cursor: 'pointer', background: c.id === activeId ? '#e6f4ff' : undefined, padding: '8px 12px' }}
        >
          <List.Item.Meta
            title={(
              <Space>
                <span>{c.user?.name}</span>
                {c.unread_count > 0 && <Badge count={c.unread_count} />}
              </Space>
            )}
            // last_message_preview is computed by the backend (build_preview);
            // deriving a label here too would let the list and the thread disagree.
            description={<Text type="secondary" ellipsis>{c.last_message_preview}</Text>}
          />
          <Text type="secondary" style={{ fontSize: 11, whiteSpace: 'nowrap' }}>
            {stamp(c.last_message_at)}
          </Text>
        </List.Item>
      )}
    />
  );
};

export default ConversationList;
