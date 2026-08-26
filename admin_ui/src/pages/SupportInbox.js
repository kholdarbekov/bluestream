// admin_ui/src/pages/SupportInbox.js
import React, { useState, useMemo, useEffect, useRef } from 'react';
import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  Row, Col, Card, Input, Button, Space, Empty, Modal, Select, message, Spin, Typography,
} from 'antd';
import { PlusOutlined, MessageOutlined } from '@ant-design/icons';
import adminService from '../services/adminService';
import { extractApiErrorMessages } from '../utils/apiError';
import ConversationList from '../components/support/ConversationList';
import MessageThread from '../components/support/MessageThread';
import SupportAttachment from '../components/support/SupportAttachment';
import LocationBubble from '../components/support/LocationBubble';
import SupportComposer from '../components/support/SupportComposer';

const { TextArea } = Input;
const { Text } = Typography;

const SupportInbox = () => {
  const { t } = useTranslation('common');
  const queryClient = useQueryClient();

  const [search, setSearch] = useState('');
  const [searchInput, setSearchInput] = useState('');
  const [activeId, setActiveId] = useState(null);
  const [newOpen, setNewOpen] = useState(false);
  const [newUserId, setNewUserId] = useState(null);
  const [newContent, setNewContent] = useState('');
  const [userSearch, setUserSearch] = useState('');
  const userDebounce = useRef();

  // Conversation list (polls every 15s)
  const { data: convData, isLoading: convLoading } = useQuery({
    queryKey: ['support-conversations', search],
    queryFn: () => adminService.getSupportConversations({ search: search || undefined, per_page: 50 }),
    refetchInterval: 15000,
    placeholderData: keepPreviousData,
  });
  const conversations = convData?.data?.items || [];

  // Thread (polls every 8s while open)
  const { data: threadData } = useQuery({
    queryKey: ['support-thread', activeId],
    queryFn: () => adminService.getSupportThread(activeId, { per_page: 100 }),
    enabled: Boolean(activeId),
    refetchInterval: activeId ? 8000 : false,
  });
  const messages = threadData?.data?.items || [];
  const activeConv = threadData?.data?.conversation || conversations.find((c) => c.id === activeId);

  // Mark read when opening a conversation
  const markReadMutation = useMutation({
    mutationFn: (id) => adminService.markSupportRead(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['support-conversations'] }),
  });
  useEffect(() => {
    if (activeId) markReadMutation.mutate(activeId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  const startMutation = useMutation({
    mutationFn: ({ userId, content }) => adminService.startSupportConversation(userId, content),
    onSuccess: (resp) => {
      message.success(t('ui.support.sent', 'Message sent'));
      setNewOpen(false);
      setNewUserId(null);
      setNewContent('');
      const newId = resp?.data?.conversation_id;
      queryClient.invalidateQueries({ queryKey: ['support-conversations'] });
      if (newId) setActiveId(newId);
    },
    onError: (error) => message.error(extractApiErrorMessages(error, t('ui.support.send_failed', 'Failed to send message'))[0]),
  });

  // User search for "new message"
  const { data: usersData, isFetching: usersFetching } = useQuery({
    queryKey: ['support-users', userSearch],
    queryFn: () => adminService.getUsers({ search: userSearch, per_page: 50 }),
    enabled: newOpen && userSearch.length >= 2,
    placeholderData: keepPreviousData,
  });
  const userOptions = useMemo(() => {
    const items = usersData?.data?.items || [];
    return items
      .filter((u) => u.telegram_id)
      .map((u) => ({ value: u.id, label: `${u.first_name || ''} ${u.last_name || ''}`.trim() + (u.phone ? ` - ${u.phone}` : '') }));
  }, [usersData]);
  const handleUserSearch = (val) => {
    if (userDebounce.current) clearTimeout(userDebounce.current);
    userDebounce.current = setTimeout(() => setUserSearch(val.trim()), 300);
  };

  const submitSearch = () => setSearch(searchInput.trim());

  const renderBody = (m) => {
    if (m.message_type === 'location') return <LocationBubble message={m} />;
    if (m.has_attachment) return <SupportAttachment message={m} />;
    // An `unsupported` message (a sticker, animation, poll, dice) has no
    // attachment and no content — without this branch it renders as an
    // empty bubble with just a timestamp, which reads as a blank message.
    if (m.message_type === 'unsupported') {
      return (
        <Text type="secondary" italic>
          {t('ui.support.unsupported_attachment', 'Unsupported attachment')}
        </Text>
      );
    }
    return null;
  };

  return (
    <Card
      title={t('ui.support.title', 'Support Inbox')}
      extra={(
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setNewOpen(true)}>
          {t('ui.support.new_message', 'New message')}
        </Button>
      )}
    >
      <Row gutter={16}>
        <Col xs={24} md={9}>
          <Space.Compact style={{ width: '100%', marginBottom: 12 }}>
            <Input
              placeholder={t('ui.support.search_placeholder', 'Search by name or phone')}
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              onPressEnter={submitSearch}
              allowClear
            />
            <Button onClick={submitSearch}>{t('ui.common.search', 'Search')}</Button>
          </Space.Compact>

          {convLoading ? (
            <Spin />
          ) : (
            <ConversationList conversations={conversations} activeId={activeId} onSelect={setActiveId} />
          )}
        </Col>

        <Col xs={24} md={15}>
          {!activeId ? (
            <Empty description={t('ui.support.select_conversation', 'Select a conversation to view messages')} />
          ) : (
            <div>
              <div style={{ fontWeight: 600, marginBottom: 8 }}>
                <MessageOutlined /> {activeConv?.user?.name}
              </div>
              <div style={{ maxHeight: 420, overflowY: 'auto', padding: 8, background: '#fafafa', borderRadius: 6 }}>
                <MessageThread messages={messages} renderBody={renderBody} />
              </div>
              <SupportComposer
                conversationId={activeId}
                onSent={() => {
                  queryClient.invalidateQueries({ queryKey: ['support-thread', activeId] });
                  queryClient.invalidateQueries({ queryKey: ['support-conversations'] });
                }}
              />
            </div>
          )}
        </Col>
      </Row>

      <Modal
        title={t('ui.support.new_message', 'New message')}
        open={newOpen}
        onCancel={() => setNewOpen(false)}
        onOk={() => startMutation.mutate({ userId: newUserId, content: newContent.trim() })}
        okButtonProps={{ disabled: !newUserId || !newContent.trim(), loading: startMutation.isPending }}
        okText={t('ui.support.send', 'Send')}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Select
            showSearch
            style={{ width: '100%' }}
            placeholder={t('ui.support.select_user', 'Select a Telegram-connected user')}
            filterOption={false}
            onSearch={handleUserSearch}
            onChange={setNewUserId}
            loading={usersFetching}
            options={userOptions}
            notFoundContent={usersFetching ? '…' : (userSearch.length < 2 ? t('ui.support.search_placeholder', 'Search by name or phone') : t('ui.support.no_conversations', 'No users found'))}
          />
          <TextArea
            placeholder={t('ui.support.message_placeholder', 'Type a message…')}
            value={newContent}
            onChange={(e) => setNewContent(e.target.value)}
            maxLength={4096}
            autoSize={{ minRows: 2, maxRows: 6 }}
          />
        </Space>
      </Modal>
    </Card>
  );
};

export default SupportInbox;
