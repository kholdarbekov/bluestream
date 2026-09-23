import React, { useState } from 'react';
import { Alert, Empty, Image, Pagination, Space, Spin, Typography } from 'antd';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';

import salesService from '../../services/salesService';
import { DEFAULT_PAGE_SIZE } from '../../utils/constants';
import { extractApiErrorMessage } from '../../utils/apiError';
import VisitPhotoThumb from './VisitPhotoThumb';

const { Text } = Typography;

// Every photo taken at one outlet, across all its visits, newest first (D27).
const OutletPhotosTab = ({ outletId }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const [page, setPage] = useState(1);
  const query = useQuery({
    queryKey: ['outletPhotos', outletId, page],
    queryFn: () => salesService.getOutletPhotos(outletId, { page, per_page: DEFAULT_PAGE_SIZE }),
    enabled: Boolean(outletId),
    placeholderData: keepPreviousData,
  });
  const photos = query.data?.photos || [];
  const total = query.data?.meta?.total || 0;

  if (query.isError) {
    return <Alert type="error" showIcon message={extractApiErrorMessage(query.error, t('ui.common.error_occurred', 'An error occurred'))} />;
  }
  if (query.isLoading) return <Spin />;
  if (!photos.length) return <Empty description={t('sales_agents:photos.empty', 'No photos yet')} />;

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Image.PreviewGroup>
        <Space wrap size="middle" align="start">
          {photos.map((photo) => (
            <Space key={photo.id} direction="vertical" size={0} align="center" data-testid="outlet-photo">
              <VisitPhotoThumb photo={photo} size={120} />
              <Text type="secondary" style={{ fontSize: 12 }}>{dayjs(photo.received_at).format('YYYY-MM-DD HH:mm')}</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>{photo.agent_name || '—'}</Text>
            </Space>
          ))}
        </Space>
      </Image.PreviewGroup>
      {total > DEFAULT_PAGE_SIZE && (
        <Pagination current={page} pageSize={DEFAULT_PAGE_SIZE} total={total} onChange={setPage} showSizeChanger={false} />
      )}
    </Space>
  );
};

export default OutletPhotosTab;
