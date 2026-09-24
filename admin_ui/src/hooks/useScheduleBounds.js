import { useQuery } from '@tanstack/react-query';

import api from '../services/api';

const fetchScheduleBounds = async () => {
  const response = await api.get('/orders/statuses');
  const data = response?.data?.data || {};
  return { minDate: data.schedule_min_date ?? null, maxDate: data.schedule_max_date ?? null };
};

/**
 * The first and last day a new delivery may be scheduled on, as the backend counts them
 * (`schedule_date_bounds`, Tashkent local). It replaces the JS copy of the horizon and the
 * browser clock that the Create Order picker used to read.
 *
 * A query of its own, refetched every time `enabled` turns true (`staleTime: 0`). It is never
 * served from the page's `['order-statuses']` entry, which is cached for a day, because a tab
 * left open overnight would offer yesterday from it. While this open's fetch is in flight the
 * bounds are null, never the previous open's, so the picker offers no day it cannot vouch for.
 *
 * @param {boolean} enabled - fetch only while the form that needs the bounds is on screen.
 * @returns {{ minDate: string|null, maxDate: string|null, isLoading: boolean }} ISO YYYY-MM-DD.
 */
const useScheduleBounds = (enabled) => {
  const isEnabled = Boolean(enabled);
  const { data, isFetching } = useQuery({
    queryKey: ['schedule-bounds'],
    queryFn: fetchScheduleBounds,
    enabled: isEnabled,
    staleTime: 0,
  });
  const fresh = isEnabled && !isFetching ? data : null;
  return {
    minDate: fresh?.minDate ?? null,
    maxDate: fresh?.maxDate ?? null,
    isLoading: isEnabled && isFetching,
  };
};

export default useScheduleBounds;
