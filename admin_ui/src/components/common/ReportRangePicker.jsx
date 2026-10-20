import React from 'react';
import { DatePicker } from 'antd';
import dayjs from 'dayjs';

const { RangePicker } = DatePicker;
const ISO_DATE = 'YYYY-MM-DD';

// Query params for a picked range. Nothing picked sends nothing, so the backend's
// own default window answers.
export const rangeParams = (range) => (
  range ? { start_date: range[0].format(ISO_DATE), end_date: range[1].format(ISO_DATE) } : {}
);

/**
 * Date range for a report whose DEFAULT window is the backend's decision.
 *
 * `range` is what the user picked (null until they pick); `answered` is the
 * `{ start_date, end_date }` the last response reported. The picker shows
 * `answered` until a pick, so the default lives in one place, server-side.
 * Clearing goes back to that default.
 */
const ReportRangePicker = ({ range, answered, onChange, ...rest }) => {
  const shown = range || (answered?.start_date && answered?.end_date
    ? [dayjs(answered.start_date), dayjs(answered.end_date)]
    : null);

  return (
    <RangePicker
      value={shown}
      format={ISO_DATE}
      onChange={(value) => onChange(value?.[0] && value?.[1] ? value : null)}
      {...rest}
    />
  );
};

export default ReportRangePicker;
