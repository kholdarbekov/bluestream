import React, { useState } from 'react';
import { Button, Image, Space, Typography } from 'antd';
import { DownloadOutlined, PlayCircleOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';

import adminService from '../../services/adminService';
import useAuthedObjectUrl from '../../hooks/useAuthedObjectUrl';

const { Text } = Typography;

const VIDEO_TYPES = new Set(['video', 'video_note']);

// Extension for a saved file Telegram sent without a name (voice notes, round videos).
const EXTENSION_BY_MIME = {
  'video/mp4': 'mp4',
  'video/quicktime': 'mov',
  'audio/ogg': 'ogg',
  'audio/mpeg': 'mp3',
};

const formatSize = (bytes) => {
  if (!bytes) return null;
  const kb = Math.max(1, Math.round(bytes / 1024));
  if (kb < 1024) return `${kb} KB`;
  const mb = bytes / (1024 * 1024);
  return `${mb >= 10 ? Math.round(mb) : Math.round(mb * 10) / 10} MB`;
};

const downloadName = (message) => message.attachment_file_name
  || [`${message.message_type}-${message.id}`, EXTENSION_BY_MIME[message.attachment_mime_type]]
    .filter(Boolean).join('.');

const SupportAttachment = ({ message }) => {
  const { t } = useTranslation('common');
  // A video can be 20 MB through the Pi, so it downloads only when the admin asks to play it.
  const isVideo = VIDEO_TYPES.has(message.message_type);
  const [playRequested, setPlayRequested] = useState(false);
  const [cantPlay, setCantPlay] = useState(false);

  // The 20 MB Bot-API-download rule is decided ONCE, server-side
  // (`TELEGRAM_MAX_DOWNLOAD_BYTES` in support_attachment_service.py) and
  // published on the message as `attachment_too_large`. Do not re-derive the
  // threshold here — that would be two places deciding one answer.
  const tooLarge = Boolean(message.attachment_too_large);

  const { url: objectUrl, failed, errorStatus, loading, retry } = useAuthedObjectUrl(
    message.id,
    (signal) => adminService.getSupportAttachmentBlob(message.id, { signal }),
    Boolean(message.has_attachment) && !tooLarge && (!isVideo || playRequested),
  );

  if (!message.has_attachment) return null;

  if (tooLarge || errorStatus === 413) {
    return (
      <Text type="secondary">
        {t('ui.support.attachment_too_large', {
          defaultValue: 'Attachment is too large for Telegram to serve (over 20 MB)',
        })}
      </Text>
    );
  }
  if (failed && errorStatus === 404) {
    return (
      <Text type="secondary">
        {t('ui.support.attachment_unavailable', { defaultValue: 'Attachment is unavailable' })}
      </Text>
    );
  }
  if (failed) {
    // A timeout, a dropped connection or a 5xx: the file may well still be there.
    return (
      <Space size={8} wrap>
        <Text type="secondary">
          {t('ui.support.attachment_load_failed', { defaultValue: "Couldn't load the attachment" })}
        </Text>
        <Button size="small" onClick={retry}>
          {t('ui.support.retry', { defaultValue: 'Retry' })}
        </Button>
      </Space>
    );
  }
  if (cantPlay) {
    // The browser could not decode it (e.g. an HEVC .mov); the admin can still open the file.
    return (
      <Space size={8} wrap>
        <Text type="secondary">
          {t('ui.support.media_cant_play', { defaultValue: "This file can't play here" })}
        </Text>
        <Button icon={<DownloadOutlined />} href={objectUrl} download={downloadName(message)} size="small">
          {t('ui.support.download', { defaultValue: 'Download' })}
        </Button>
      </Space>
    );
  }

  if (isVideo && !objectUrl) {
    const size = formatSize(message.attachment_size);
    return (
      <Button icon={<PlayCircleOutlined />} loading={loading} onClick={() => setPlayRequested(true)}>
        {size
          ? t('ui.support.play_video_size', { defaultValue: 'Play video · {{size}}', size })
          : t('ui.support.play_video', { defaultValue: 'Play video' })}
      </Button>
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
  if (isVideo) {
    return (
      <video
        src={objectUrl}
        controls
        autoPlay
        preload="metadata"
        onError={() => setCantPlay(true)}
        style={{ maxWidth: 260, borderRadius: 6 }}
      />
    );
  }
  if (message.message_type === 'voice' || message.message_type === 'audio') {
    return <audio src={objectUrl} controls preload="metadata" onError={() => setCantPlay(true)} />;
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
