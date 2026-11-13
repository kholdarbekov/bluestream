import React, { useState } from 'react';
import { Alert, Card, Empty, Space, Spin, Table, Tag, Typography } from 'antd';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import adminService from '../services/adminService';
import ReportRangePicker, { rangeParams } from '../components/common/ReportRangePicker';
import { formatDate } from '../utils/dateUtils';

const { Title, Text } = Typography;

const DASH = '—';
const hasNumber = (value) => typeof value === 'number' && Number.isFinite(value);

// Shares arrive as 0-1 floats and minutes as floats; a figure the backend could not compute (None) is a dash,
// never NaN or a 0 that would pass for a real measurement.
const percent = (share) => (hasNumber(share) ? `${Math.round(share * 100)}%` : DASH);
const minutes = (value) => (hasNumber(value) ? `${Math.round(value)} min` : DASH);
const count = (value) => (hasNumber(value) ? String(value) : DASH);
// A threshold the report publishes in `definitions` (the morning gate, the daytime hours, the "on the way" tolerance):
// the page names it, never states its own.
const threshold = (value) => (value === undefined || value === null ? DASH : String(value));

// "11-13" before "13-15", "6-10" before "11-15": bands sort by their lower edge. The monitoring bands arrive as JSON
// objects, whose keys the backend's JSON provider sorts as strings ("11-13" before "7-9").
const byLowerEdge = ([a], [b]) => parseFloat(a) - parseFloat(b);
const monitoringBands = (bands) => Object.entries(bands || {})
  .sort(byLowerEdge)
  .map(([band, { ratio, n }]) => `${band}: ${hasNumber(ratio) ? ratio.toFixed(2) : DASH} (${count(n)})`)
  .join(', ') || DASH;

const EtaAccuracy = () => {
  const { t } = useTranslation('delivery');
  const [range, setRange] = useState(null);

  const { data: report, isLoading, isError } = useQuery({
    queryKey: ['etaAccuracy', rangeParams(range)],
    queryFn: () => adminService.getEtaAccuracy(rangeParams(range)),
  });

  const columns = [
    {
      title: t('eta_acc_col_stops_ahead', { defaultValue: 'Stops ahead' }),
      dataIndex: 'stops_ahead_band',
    },
    {
      title: t('eta_acc_col_started', { defaultValue: 'Driver started' }),
      dataIndex: 'started',
      render: (started) => (started
        ? t('eta_acc_yes', { defaultValue: 'Yes' })
        : t('eta_acc_no', { defaultValue: 'No' })),
    },
    {
      title: t('eta_acc_col_promises', { defaultValue: 'Promises' }),
      dataIndex: 'n',
      render: count,
    },
    {
      title: t('eta_acc_col_kept', { defaultValue: 'Kept' }),
      dataIndex: 'kept_share',
      render: percent,
    },
    {
      title: t('eta_acc_col_median_window', { defaultValue: 'Median window' }),
      dataIndex: 'median_window_minutes',
      render: minutes,
    },
  ];

  const renderReport = ({
    gate, by_bucket: buckets, median_window_minutes: windows, by_drivers_working: drivers,
    change_messages: changes, s2, s2_stale_estimate: stale, latest_fit: fit, definitions,
  }) => {
    if (!gate?.sample) {
      return <Empty description={t('eta_acc_empty', { defaultValue: 'No delivery promises were recorded in this period yet' })} />;
    }
    const { excluded = {} } = gate;
    const monitoring = fit?.monitoring;
    return (
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Card>
          <Space direction="vertical" size="small">
            <Space>
              <Text strong>
                {t('eta_acc_gate', {
                  defaultValue: 'Go-live check: {{kept}} kept over {{n}} orders (target {{target}})',
                  kept: percent(gate.kept_share),
                  n: count(gate.n),
                  target: percent(gate.target),
                })}
              </Text>
              {gate.passes
                ? <Tag color="green">{t('eta_acc_passing', { defaultValue: 'Passing' })}</Tag>
                : <Tag color="orange">{t('eta_acc_not_yet', { defaultValue: 'Not yet' })}</Tag>}
            </Space>
            <Text>
              {t('eta_acc_before_eleven', {
                defaultValue: 'Orders placed before {{morning_ends}}: {{kept}} over {{n}}',
                morning_ends: threshold(definitions?.morning_ends),
                kept: percent(gate.before_eleven?.kept_share),
                n: count(gate.before_eleven?.n),
              })}
            </Text>
            <Text type="secondary">
              {t('eta_acc_excluded', {
                defaultValue: 'Not counted: {{dated}} booked for a later date, {{no_window}} without a time, {{cancelled}} cancelled, {{returned}} returned, {{non_driver}} closed by staff, {{open}} still open',
                dated: count(excluded.dated),
                no_window: count(excluded.no_window),
                cancelled: count(excluded.cancelled),
                returned: count(excluded.returned),
                non_driver: count(excluded.non_driver_close),
                open: count(excluded.open),
              })}
            </Text>
          </Space>
        </Card>

        <Table
          rowKey={(row) => `${row.stops_ahead_band}-${row.started}`}
          columns={columns}
          dataSource={buckets || []}
          pagination={false}
          size="small"
        />

        <Space direction="vertical" size="small">
          <Text>
            {t('eta_acc_median_window', {
              defaultValue: 'Median window: {{minutes}} overall, {{minutes_day}} for orders placed {{daytime_from}}–{{daytime_to}} (aim: 3 hours)',
              daytime_from: threshold(definitions?.daytime_from),
              daytime_to: threshold(definitions?.daytime_to),
              minutes: minutes(windows?.overall),
              minutes_day: minutes(windows?.placed_7_to_15),
            })}
          </Text>
          <Text>
            {t('eta_acc_same_day', {
              defaultValue: 'Delivered the same day as promised: {{share}}',
              share: percent(report.today_delivered_today_share),
            })}
          </Text>
          <Text>
            {t('eta_acc_tomorrow_same_day', {
              defaultValue: 'Promised tomorrow but delivered the same day: {{share}}',
              share: percent(report.tomorrow_delivered_same_day_share),
            })}
          </Text>
          <Text>
            {t('eta_acc_first_half_same_day', {
              defaultValue: 'Orders placed before {{first_half_ends}} and delivered the same day: {{share}} over {{n}}',
              first_half_ends: threshold(definitions?.first_half_ends),
              share: percent(report.first_half_day_same_day?.share),
              n: count(report.first_half_day_same_day?.n),
            })}
          </Text>
          <Text>
            {t('eta_acc_early', {
              defaultValue: 'Arrived before the window opened: {{share}}, by a median {{minutes}}',
              share: percent(report.early_share),
              minutes: minutes(report.median_minutes_early),
            })}
          </Text>
          <Text>
            {t('eta_acc_drivers', {
              defaultValue: 'One driver working: {{kept}} kept, median window {{minutes}}. Two or more: {{kept2}}, {{minutes2}}',
              kept: percent(drivers?.one?.kept_share),
              minutes: minutes(drivers?.one?.median_window_minutes),
              kept2: percent(drivers?.two_or_more?.kept_share),
              minutes2: minutes(drivers?.two_or_more?.median_window_minutes),
            })}
          </Text>
          <Text>
            {t('eta_acc_updates', {
              defaultValue: 'Updates per order: none {{zero}}, at most one {{one}}',
              zero: percent(changes?.none_share),
              one: percent(changes?.at_most_one_share),
            })}
          </Text>
          <Text>
            {t('eta_acc_on_the_way', {
              defaultValue: 'On the way: kept {{kept}}, within {{tolerance}} minutes {{within}}, median error Google {{google}} vs our engine {{engine}}',
              tolerance: threshold(definitions?.s2_tolerance_minutes),
              kept: percent(s2?.kept_share),
              within: percent(s2?.within_15_share),
              google: minutes(s2?.google_median_error_minutes),
              engine: minutes(s2?.engine_median_error_minutes),
            })}
          </Text>
          <Text>
            {t('eta_acc_on_the_way_stale', {
              defaultValue: 'On the way from a position too old for Google ({{n}}): kept {{kept}}, within {{tolerance}} minutes {{within}}, median error of our engine {{engine}}',
              n: count(stale?.n),
              tolerance: threshold(definitions?.s2_tolerance_minutes),
              kept: percent(stale?.kept_share),
              within: percent(stale?.within_15_share),
              engine: minutes(stale?.engine_median_error_minutes),
            })}
          </Text>
          {monitoring && (
            <Text>
              {t('eta_acc_curve', {
                defaultValue: 'Stop time against the curve, by time of day: {{bands}}. By order size: {{sizes}}',
                bands: monitoringBands(monitoring.by_hour_band),
                sizes: monitoringBands(monitoring.by_bottles_band),
              })}
            </Text>
          )}
          <Text type="secondary">
            {t('eta_acc_latest_fit', {
              defaultValue: 'Latest fit: {{date}}',
              date: fit?.fitted_at ? formatDate(fit.fitted_at, 'YYYY-MM-DD') : DASH,
            })}
          </Text>
        </Space>
      </Space>
    );
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Title level={3} style={{ margin: 0 }}>
        {t('eta_acc_title', { defaultValue: 'Delivery time accuracy' })}
      </Title>
      <ReportRangePicker range={range} answered={report} onChange={setRange} />
      {isLoading && <Spin />}
      {isError && (
        <Alert type="error" showIcon message={t('eta_acc_load_failed', { defaultValue: 'The report could not be loaded' })} />
      )}
      {report && renderReport(report)}
    </Space>
  );
};

export default EtaAccuracy;
