import React from 'react';
import { Card, Statistic, Tooltip } from 'antd';
import {
  ArrowUpOutlined,
  ArrowDownOutlined,
  InfoCircleOutlined
} from '@ant-design/icons';

const StatCard = ({
  title,
  value,
  suffix,
  prefix,
  trend,
  trendValue,
  icon,
  color = '#1890ff',
  loading = false,
  tooltip,
  extra
}) => {
  const getTrendIcon = () => {
    if (trend === 'up') return <ArrowUpOutlined />;
    if (trend === 'down') return <ArrowDownOutlined />;
    return null;
  };

  const getTrendColor = () => {
    if (trend === 'up') return '#52c41a';
    if (trend === 'down') return '#ff4d4f';
    return '#666';
  };

  return (
    <Card
      loading={loading}
      className="dashboard-card stat-card"
      style={{ borderLeft: `4px solid ${color}` }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div style={{ flex: 1 }}>
          <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
            <span className="stat-title">{title}</span>
            {tooltip && (
              <Tooltip title={tooltip}>
                <InfoCircleOutlined style={{ marginLeft: 4, color: '#999' }} />
              </Tooltip>
            )}
          </div>

          <Statistic
            value={value}
            suffix={suffix}
            prefix={prefix}
            valueStyle={{
              fontSize: 28,
              fontWeight: 'bold',
              color,
              marginBottom: 8
            }}
          />

          {(trend || trendValue) && (
            <div className="stat-trend" style={{ color: getTrendColor() }}>
              {getTrendIcon()}
              {trendValue && <span style={{ marginLeft: 4 }}>{trendValue}</span>}
            </div>
          )}
        </div>

        {icon && (
          <div style={{
            fontSize: 24,
            color,
            opacity: 0.8,
            marginLeft: 16
          }}>
            {icon}
          </div>
        )}

        {extra && (
          <div style={{ marginLeft: 16 }}>
            {extra}
          </div>
        )}
      </div>
    </Card>
  );
};

export default StatCard;