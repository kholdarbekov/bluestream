import React from 'react';
import { Image, Space, Spin, Tag, Typography } from 'antd';
import { useTranslation } from 'react-i18next';

import salesService from '../../services/salesService';
import useAuthedObjectUrl from '../../hooks/useAuthedObjectUrl';

const { Text } = Typography;

// One visit photo (D27). The picture lives on Telegram and the backend resolves its file id from
// the row, so this component only ever sends a photo id. A photo Telegram no longer serves is a
// placeholder, never a broken page.
const VisitPhotoThumb = ({ photo, size = 96 }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const { url, failed } = useAuthedObjectUrl(photo.id, () => salesService.getVisitPhotoBlob(photo.id));
  const kindLabel = t(`sales_agents:photos.kind.${photo.kind}`, photo.kind);
  const box = {
    width: size, height: size, display: 'flex', alignItems: 'center', justifyContent: 'center',
    borderRadius: 6, background: 'rgba(0, 0, 0, 0.04)',
  };

  let body;
  if (failed) {
    body = <div style={box}><Text type="secondary" style={{ fontSize: 12, textAlign: 'center' }}>{t('sales_agents:photos.unavailable', 'Photo unavailable')}</Text></div>;
  } else if (!url) {
    body = <div style={box}><Spin size="small" /></div>;
  } else {
    body = <Image src={url} alt={kindLabel} width={size} height={size} style={{ objectFit: 'cover', borderRadius: 6 }} />;
  }

  return (
    <Space direction="vertical" size={2} align="center">
      {body}
      <Space size={4}>
        <Text type="secondary" style={{ fontSize: 12 }}>{kindLabel}</Text>
        {photo.is_duplicate && <Tag color="red">{t('sales_agents:photos.duplicate', 'Duplicate')}</Tag>}
      </Space>
    </Space>
  );
};

export default VisitPhotoThumb;
