import dayjs from 'dayjs';

// The backend publishes ONE `upcoming_delivery` ({date, ..., time_label}) and the subscription's own `time_label`;
// this only prints the date and the words it gave (the window, "Any time" or "Time to be confirmed"). A DATE is a
// calendar label, so it is formatted as written, never converted between zones.
export function formatUpcomingDelivery(subscription) {
  const upcoming = subscription?.upcoming_delivery;
  const day = upcoming?.date || subscription?.next_delivery_date;
  if (!day) return '-';
  const label = dayjs(day, 'YYYY-MM-DD').format('MMM DD, YYYY');
  const timeLabel = upcoming?.time_label || subscription?.time_label;
  return timeLabel ? `${label}, ${timeLabel}` : label;
}
