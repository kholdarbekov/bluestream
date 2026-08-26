import React, { useRef, useState } from 'react';
import { Button, Input, Popover, Space, Tag, message as antdMessage } from 'antd';
import { EnvironmentOutlined, PaperClipOutlined, SendOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';

import adminService from '../../services/adminService';
import { extractApiErrorMessages } from '../../utils/apiError';
import parseCoordinates from '../../utils/parseCoordinates';

const { TextArea } = Input;

const SupportComposer = ({ conversationId, onSent }) => {
  const { t } = useTranslation('common');
  const [text, setText] = useState('');
  const [file, setFile] = useState(null);
  const [coords, setCoords] = useState('');
  const [pinOpen, setPinOpen] = useState(false);
  const [sending, setSending] = useState(false);
  const fileInput = useRef();

  // Shared with sendPin below, which must NOT clear text/file: a pin is a
  // side send, not a replacement for a draft reply or a staged attachment.
  const notifyDelivery = (response) => {
    if (response?.data?.delivery?.success) {
      antdMessage.success(t('ui.support.sent', { defaultValue: 'Message sent' }));
    } else {
      antdMessage.warning(t('ui.support.delivery_failed', { defaultValue: 'Not delivered' }));
    }
    onSent();
  };

  const resetFile = () => {
    setFile(null);
    if (fileInput.current) fileInput.current.value = '';
  };

  const finish = (response) => {
    notifyDelivery(response);
    setText('');
    resetFile();
  };

  const send = async () => {
    setSending(true);
    try {
      // A chosen file makes the typed text its caption, so one Send does one thing.
      const response = file
        ? await adminService.sendSupportAttachment(conversationId, file, text.trim() || undefined)
        : await adminService.replySupportMessage(conversationId, text.trim());
      finish(response);
    } catch (error) {
      antdMessage.error(extractApiErrorMessages(error, t('ui.support.send_failed', { defaultValue: 'Failed to send message' }))[0]);
    } finally {
      setSending(false);
    }
  };

  const sendPin = async () => {
    // parseCoordinates returns {latitude, longitude} — NOT {lat, lng}.
    const parsed = parseCoordinates(coords);
    if (!parsed) {
      antdMessage.error(t('ui.support.bad_coordinates', { defaultValue: 'Could not read those coordinates' }));
      return;
    }
    setSending(true);
    try {
      const response = await adminService.sendSupportLocation(conversationId, parsed.latitude, parsed.longitude);
      setPinOpen(false);
      setCoords('');
      notifyDelivery(response);
    } catch (error) {
      antdMessage.error(extractApiErrorMessages(error, t('ui.support.send_failed', { defaultValue: 'Failed to send message' }))[0]);
    } finally {
      setSending(false);
    }
  };

  const pinForm = (
    <Space direction="vertical">
      <Input
        placeholder="41.32, 69.24"
        value={coords}
        onChange={(e) => setCoords(e.target.value)}
        style={{ width: 220 }}
      />
      <Button type="primary" size="small" onClick={sendPin} loading={sending}>
        {t('ui.support.send_pin', { defaultValue: 'Send pin' })}
      </Button>
    </Space>
  );

  return (
    <Space direction="vertical" style={{ width: '100%', marginTop: 12 }}>
      {file && <Tag closable onClose={resetFile}>{file.name}</Tag>}
      <Space.Compact style={{ width: '100%' }}>
        <TextArea
          placeholder={t('ui.support.message_placeholder', { defaultValue: 'Type a message…' })}
          value={text}
          onChange={(e) => setText(e.target.value)}
          maxLength={4096}
          autoSize={{ minRows: 1, maxRows: 4 }}
        />
        <Button
          icon={<PaperClipOutlined />}
          onClick={() => fileInput.current?.click()}
          title={t('ui.support.attach', { defaultValue: 'Attach a file' })}
          aria-label={t('ui.support.attach', { defaultValue: 'Attach a file' })}
        />
        <Popover open={pinOpen} onOpenChange={setPinOpen} trigger="click" content={pinForm}>
          {/* Distinct wording from the submit button below: both are
              accessible-named buttons, and "Send pin" on this trigger would
              collide with the composer's own "Send" button under any
              case-insensitive /Send/ match. */}
          <Button icon={<EnvironmentOutlined />} aria-label={t('ui.support.attach_pin', { defaultValue: 'Attach a pin' })} />
        </Popover>
        <Button
          type="primary"
          icon={<SendOutlined />}
          loading={sending}
          disabled={!text.trim() && !file}
          onClick={send}
        >
          {t('ui.support.send', { defaultValue: 'Send' })}
        </Button>
      </Space.Compact>
      <input
        ref={fileInput}
        data-testid="support-file-input"
        type="file"
        // Guides the picker toward the app's `ALLOWED_EXTENSIONS` allowlist
        // (business_app/config/base.py) — a UX hint only. The backend is the
        // real gate: it 400s a disallowed type regardless of what the picker
        // let through.
        //
        // Narrowed to the intersection valid in every environment:
        // production.py:150 restricts ALLOWED_EXTENSIONS to
        // {"png","jpg","jpeg","pdf"} only, dropping gif/doc/docx that
        // base.py otherwise allows. Offering those wider types here would
        // let an admin pick a file production immediately 400s.
        accept=".png,.jpg,.jpeg,.pdf"
        style={{ display: 'none' }}
        onChange={(e) => setFile(e.target.files?.[0] || null)}
      />
    </Space>
  );
};

export default SupportComposer;
