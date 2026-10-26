import dayjs from 'dayjs';

// The backend publishes ONE `upcoming_delivery` ({date, window_start, window_end, order_id}); this only
// formats it. A DATE is a calendar label, so it is formatted as written, never converted between zones.
export function formatUpcomingDelivery(subscription, timeToBeConfirmed = 'time to be confirmed') {
  const upcoming = subscription?.upcoming_delivery;
  const day = upcoming?.date || subscription?.next_delivery_date;
  if (!day) return '-';
  const label = dayjs(day, 'YYYY-MM-DD').format('MMM DD, YYYY');
  if (upcoming?.window_start && upcoming?.window_end) {
    return `${label}, ${upcoming.window_start}-${upcoming.window_end}`;
  }
  return `${label} (${timeToBeConfirmed})`;
}
