import React from 'react';
import { render, screen } from '@testing-library/react';

import MessageThread, { groupMessagesByDay } from '../../../components/support/MessageThread';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (opts && opts.defaultValue) || (typeof opts === 'string' ? opts : key) }),
}));

const msg = (id, createdAt, extra = {}) => ({
  id,
  direction: 'inbound',
  message_type: 'text',
  content: `body ${id}`,
  created_at: createdAt,
  has_attachment: false,
  ...extra,
});

describe('MessageThread', () => {
  it('renders the time of day on every message', () => {
    // 09:05 UTC is 14:05 at the +05:00 display offset.
    render(<MessageThread messages={[msg(1, '2026-08-25T09:05:00Z')]} />);

    expect(screen.getByText('14:05')).toBeInTheDocument();
  });

  it('groups messages into one section per calendar day', () => {
    const groups = groupMessagesByDay([
      msg(1, '2026-08-23T09:00:00Z'),
      msg(2, '2026-08-23T18:00:00Z'),
      msg(3, '2026-08-25T07:00:00Z'),
    ]);

    expect(groups).toHaveLength(2);
    expect(groups[0].messages.map((m) => m.id)).toEqual([1, 2]);
    expect(groups[1].messages.map((m) => m.id)).toEqual([3]);
  });

  it('groups by the DISPLAY day, not the UTC day', () => {
    // 20:30 UTC on the 24th is 01:30 on the 25th in Tashkent. Grouping on the
    // raw UTC date would file these two under different headings even though
    // the operator saw them in one sitting.
    const groups = groupMessagesByDay([
      msg(1, '2026-08-24T20:30:00Z'),
      msg(2, '2026-08-24T21:00:00Z'),
    ]);

    expect(groups).toHaveLength(1);
  });

  it('renders a delivery-failure tag on a failed outbound message', () => {
    render(<MessageThread messages={[
      msg(1, '2026-08-25T09:00:00Z', { direction: 'outbound', delivery_status: 'failed' }),
    ]} />);

    expect(screen.getByText(/Not delivered/i)).toBeInTheDocument();
  });

  it('renders a forwarded attribution when present', () => {
    render(<MessageThread messages={[
      msg(1, '2026-08-25T09:00:00Z', { forwarded_from: 'Dilnoza K', forwarded_origin_type: 'user' }),
    ]} />);

    expect(screen.getByText(/Dilnoza K/)).toBeInTheDocument();
  });

  it('does not claim an identity for a hidden-user forward', () => {
    render(<MessageThread messages={[
      msg(1, '2026-08-25T09:00:00Z', { forwarded_from: 'Someone', forwarded_origin_type: 'hidden_user' }),
    ]} />);

    expect(screen.getByText(/hidden/i)).toBeInTheDocument();
    // FIX 10c: the old assertion only checked the generic label was present —
    // it would still pass if a regression appended the leaked name alongside
    // it. Telegram deliberately withholds the real identity on a hidden-user
    // forward, so the display name that came with the payload must never
    // render at all.
    expect(screen.queryByText(/Someone/)).toBeNull();
  });
});
