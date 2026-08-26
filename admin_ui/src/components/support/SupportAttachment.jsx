import React, { useEffect, useState } from 'react';
import { Button, Image, Typography } from 'antd';
import { DownloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';

import adminService from '../../services/adminService';

const { Text } = Typography;

const SupportAttachment = ({ message }) => {
  const { t } = useTranslation('common');
  const [objectUrl, setObjectUrl] = useState(null);
  const [failed, setFailed] = useState(false);

  // The 20 MB Bot-API-download rule is decided ONCE, server-side
  // (`TELEGRAM_MAX_DOWNLOAD_BYTES` in support_attachment_service.py) and
  // published on the message as `attachment_too_large`. Do not re-derive the
  // threshold here — that would be two places deciding one answer.
  const tooLarge = Boolean(message.attachment_too_large);

  useEffect(() => {
    if (!message.has_attachment || tooLarge) return undefined;
    let url = null;
    let cancelled = false;

    adminService.getSupportAttachmentBlob(message.id)
      .then((blob) => {
        if (cancelled) return;
        url = URL.createObjectURL(blob);
        setObjectUrl(url);
      })
      .catch(() => { if (!cancelled) setFailed(true); });

    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [message.id, message.has_attachment, tooLarge]);

  if (!message.has_attachment) return null;

  if (tooLarge) {
    return (
      <Text type="secondary">
        {t('ui.support.attachment_too_large', {
          defaultValue: 'Attachment is too large for Telegram to serve (over 20 MB)',
        })}
      </Text>
    );
  }
  if (failed) {
    return (
      <Text type="secondary">
        {t('ui.support.attachment_unavailable', { defaultValue: 'Attachment is unavailable' })}
      </Text>
    );
  }
  if (!objectUrl) return <Text type="secondary">…</Text>;

  if (message.message_type === 'photo') {
    // Click-to-zoom stays on (matches Blog.js / Products.js, the other two
    // <Image> call sites) — examining a customer's photo is the point of this
    // screen. `alt` disambiguates the real <img> from antd's preview-mask
    // "eye" icon, which also carries role="img".
    return <Image src={objectUrl} alt={message.attachment_file_name || 'Photo attachment'} style={{ maxWidth: 260, borderRadius: 6 }} />;
  }
  if (message.message_type === 'video' || message.message_type === 'video_note') {
    return <video src={objectUrl} controls preload="metadata" style={{ maxWidth: 260, borderRadius: 6 }} />;
  }
  if (message.message_type === 'voice' || message.message_type === 'audio') {
    return <audio src={objectUrl} controls preload="metadata" />;
  }
  return (
    <Button
      icon={<DownloadOutlined />}
      href={objectUrl}
      download={message.attachment_file_name || 'attachment'}
      size="small"
    >
      {message.attachment_file_name || t('ui.support.download', { defaultValue: 'Download' })}
    </Button>
  );
};

export default SupportAttachment;
