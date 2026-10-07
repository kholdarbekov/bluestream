import React, { useState } from 'react';
import { Alert, Drawer, Space, Spin, Tabs } from 'antd';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import salesPayService from '../../../services/salesPayService';
import { useAuthStore } from '../../../stores/authStore';
import { extractApiErrorMessage } from '../../../utils/apiError';
import PayDaysTable from './PayDaysTable';
import PayFormula from './PayFormula';
import PayLinesTable from './PayLinesTable';
import PayNewOutletsTable from './PayNewOutletsTable';
import PayPenaltiesAdjustments from './PayPenaltiesAdjustments';
import { dateLabel, instant, monthLabel } from './payFormat';

/**
 * One agent in one month (§6.2 "Statement drawer"): A8, live for an open month and frozen
 * otherwise, in one shape. The drill-down C10 requires; there is no export (review I12).
 */
const PayStatementDrawer = ({ month, agentId, onClose }) => {
  const { t } = useTranslation(['sales_agents', 'common']);
  const canManagePay = useAuthStore().hasPermission('can_manage_sales_pay');
  const open = Boolean(agentId);
  const [tab, setTab] = useState('summary');

  const statementQuery = useQuery({
    queryKey: ['salesPay', 'statement', month, agentId],
    queryFn: () => salesPayService.getStatement(month, agentId),
    enabled: canManagePay && open,
  });
  const statement = statementQuery.data;
  const close = () => { setTab('summary'); onClose(); };

  let body = <Spin />;
  if (statementQuery.isError) {
    body = <Alert type="error" showIcon message={extractApiErrorMessage(statementQuery.error, t('ui.common.error_occurred', 'An error occurred'))} />;
  } else if (statement) {
    body = (
      <Space direction="vertical" style={{ width: '100%' }}>
        {/* D-Q11: every admin sees which inputs the agent decided about their own pay. */}
        {statement.self_decided ? (
          <Alert
            type="warning"
            showIcon
            message={t('sales_agents:pay.self_decided.alert', { defaultValue: 'This statement includes decisions {{name}} made about their own pay:', name: statement.agent.name })}
            description={(
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {statement.self_decisions.map((row) => (
                  <li key={`${row.input}-${row.id}-${row.action}`}>
                    {`${t(`sales_agents:pay.self_decided.input.${row.input}`, row.input)} · ${t(`sales_agents:pay.self_decided.action.${row.action}`, row.action)} · ${row.date ? dateLabel(row.date) : instant(row.at)}`}
                  </li>
                ))}
              </ul>
            )}
          />
        ) : null}
        <Tabs
          activeKey={tab}
          onChange={setTab}
          items={[
            { key: 'summary', label: t('sales_agents:pay.drawer.summary', 'Summary'), children: <PayFormula statement={statement} onOpenTab={setTab} /> },
            {
              key: 'orders',
              label: t('sales_agents:pay.drawer.orders', 'Orders'),
              children: <PayLinesTable month={month} agentId={agentId} lineKinds={statement.line_kinds} active={open && tab === 'orders'} />,
            },
            { key: 'days', label: t('sales_agents:pay.drawer.days', 'Days'), children: <PayDaysTable statement={statement} agentId={agentId} /> },
            { key: 'new_outlets', label: t('sales_agents:pay.drawer.new_outlets', 'New outlets'), children: <PayNewOutletsTable rows={statement.new_outlets} /> },
            {
              key: 'penalties',
              label: t('sales_agents:pay.drawer.penalties', 'Penalties & adjustments'),
              children: <PayPenaltiesAdjustments statement={statement} agentId={agentId} />,
            },
          ]}
        />
      </Space>
    );
  }

  return (
    <Drawer
      open={open}
      width={760}
      onClose={close}
      destroyOnHidden
      title={statement ? `${statement.agent.name} · ${monthLabel(statement.month)}` : ''}
    >
      {open ? body : null}
    </Drawer>
  );
};

export default PayStatementDrawer;
