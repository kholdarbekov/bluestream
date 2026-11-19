import React from 'react';
import { Image, Spin, Typography } from 'antd';
import { useTranslation } from 'react-i18next';

import driverExpenseService from '../../services/driverExpenseService';
import useAuthedObjectUrl from '../../hooks/useAuthedObjectUrl';

const { Text } = Typography;

// One expense receipt photo. The picture lives on Telegram and the backend resolves its file from the expense row, so
// this component only ever sends an expense id. A photo Telegram no longer serves is a placeholder, never a broken page.
const ExpenseReceiptThumb = ({ expenseId, size = 120 }) => {
  const { t } = useTranslation();
  const { url, failed } = useAuthedObjectUrl(expenseId, () => driverExpenseService.getReceiptBlob(expenseId));
  const box = {
    width: size, height: size, display: 'flex', alignItems: 'center', justifyContent: 'center',
    borderRadius: 6, background: 'rgba(0, 0, 0, 0.04)',
  };

  if (failed) {
    return (
      <div style={box}>
        <Text type="secondary" style={{ fontSize: 12, textAlign: 'center' }}>
          {t('ui.driver_expenses.photo_unavailable', 'Photo unavailable')}
        </Text>
      </div>
    );
  }
  if (!url) return <div style={box}><Spin size="small" /></div>;
  return <Image src={url} alt="" width={size} height={size} style={{ objectFit: 'cover', borderRadius: 6 }} />;
};

export default ExpenseReceiptThumb;
