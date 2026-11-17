import React, { useState } from 'react';
import { Alert, Button, Empty, Space, Table, Typography } from 'antd';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import adminService from '../services/adminService';
import PriceChangeDrawer, { StageTag } from '../components/priceChange/PriceChangeDrawer';
import { NewPriceChangeButton, PriceChangeActions } from '../components/priceChange/PriceChangeFormModal';
import { PRICE_CHANGES_KEY } from '../utils/constants';
import { formatCalendarDate } from '../utils/dateUtils';
import { formatMoneyUZS } from '../utils/formatMoney';

const { Title, Paragraph } = Typography;

// The stages the backend publishes (`PRICE_CHANGE_STAGES`); the drawer labels each one.
export const STAGES = ['scheduled', 'announcing', 'announced', 'reminded', 'applied', 'window_closed', 'cancelled'];

// "<product>: <old price> → <new price>". `old_price` is set once the switch has applied, so an applied row keeps its
// history; before that the old price is the list price now.
const productLine = (item) => `${item.product_name}: ${formatMoneyUZS(item.old_price ?? item.current_price)} → ${formatMoneyUZS(item.new_price)}`;

const PriceChanges = () => {
  const { t } = useTranslation();
  const [openId, setOpenId] = useState(null);
  const { data, isLoading, isError } = useQuery({
    queryKey: [...PRICE_CHANGES_KEY, 'list'],
    queryFn: () => adminService.listPriceChanges(),
  });

  const columns = [
    { title: t('ui.price_changes.col_stage', 'Stage'), dataIndex: 'stage', render: (stage) => <StageTag stage={stage} /> },
    {
      title: t('ui.price_changes.col_announce_on', 'Announcement'),
      dataIndex: 'announce_on',
      render: formatCalendarDate,
    },
    {
      title: t('ui.price_changes.col_change_on', 'New prices from'),
      dataIndex: 'change_on',
      render: formatCalendarDate,
    },
    {
      title: t('ui.price_changes.col_products', 'Products'),
      dataIndex: 'items',
      render: (items) => (
        <Space direction="vertical" size={0}>
          {items.map((item) => <span key={item.product_id}>{productLine(item)}</span>)}
        </Space>
      ),
    },
    {
      title: '',
      key: 'details',
      render: (_, row) => (
        <Space size="small">
          <Button size="small" onClick={() => setOpenId(row.id)}>
            {t('ui.price_changes.details', 'Details')}
          </Button>
          <PriceChangeActions priceChange={row} />
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between' }}>
        <Title level={3}>{t('ui.price_changes.title', 'Price changes')}</Title>
        <NewPriceChangeButton action={data?.allowed_actions?.create} />
      </Space>
      <Paragraph type="secondary">
        {t('ui.price_changes.hint', 'Schedule a price change and tell customers ahead of time. New prices start at 00:00 Tashkent time on the change date.')}
      </Paragraph>
      {isError && <Alert type="error" message={t('ui.price_changes.list_failed', 'Could not load the price changes')} />}
      <Table
        rowKey="id"
        columns={columns}
        dataSource={data?.items || []}
        loading={isLoading}
        pagination={false}
        locale={{ emptyText: <Empty description={t('ui.price_changes.empty', 'No price changes yet')} /> }}
      />
      <PriceChangeDrawer priceChangeId={openId} onClose={() => setOpenId(null)} />
    </div>
  );
};

export default PriceChanges;
