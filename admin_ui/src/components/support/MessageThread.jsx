import React from 'react';
import { Divider, Empty } from 'antd';
import { useTranslation } from 'react-i18next';

import { toTashkent } from '../../utils/dateUtils';
import MessageBubble from './MessageBubble';

/**
 * One section per calendar day, in the DISPLAY timezone.
 *
 * Grouping on the raw UTC date would split an evening's conversation across two
 * headings, because 20:00 UTC is already tomorrow in Tashkent.
 */
export const groupMessagesByDay = (messages = []) => {
  const groups = [];
  messages.forEach((message) => {
    const dayKey = toTashkent(message.created_at).format('YYYY-MM-DD');
    const last = groups[groups.length - 1];
    if (last && last.dayKey === dayKey) last.messages.push(message);
    else groups.push({ dayKey, messages: [message] });
  });
  return groups;
};

const MessageThread = ({ messages = [], renderBody }) => {
  const { t } = useTranslation('common');

  if (messages.length === 0) {
    return <Empty description={t('ui.support.empty_thread', { defaultValue: 'No messages in this conversation' })} />;
  }

  const today = toTashkent(new Date()).format('YYYY-MM-DD');
  const yesterday = toTashkent(new Date()).subtract(1, 'day').format('YYYY-MM-DD');

  // Labelled from the group's first MESSAGE, never by re-parsing dayKey.
  // `toTashkent('2026-08-23')` would read that string as local midnight and
  // then shift it to +05:00, moving the printed date by a day in some browser
  // timezones — the exact bug the display-day grouping above exists to avoid.
  const dayLabel = (group) => {
    if (group.dayKey === today) return t('ui.support.today', { defaultValue: 'Today' });
    if (group.dayKey === yesterday) return t('ui.support.yesterday', { defaultValue: 'Yesterday' });
    return toTashkent(group.messages[0].created_at).format('DD MMM YYYY');
  };

  return (
    <div>
      {groupMessagesByDay(messages).map((group) => (
        <div key={group.dayKey}>
          <Divider plain style={{ fontSize: 12, margin: '8px 0' }}>{dayLabel(group)}</Divider>
          {group.messages.map((message) => (
            <MessageBubble key={message.id} message={message}>
              {renderBody ? renderBody(message) : null}
            </MessageBubble>
          ))}
        </div>
      ))}
    </div>
  );
};

export default MessageThread;
