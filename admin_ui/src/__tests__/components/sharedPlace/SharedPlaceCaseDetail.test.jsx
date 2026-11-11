import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { message } from 'antd';

import SharedPlaceCaseDetail from '../../../components/sharedPlace/SharedPlaceCaseDetail';
import adminService from '../../../services/adminService';
import { useAuthStore } from '../../../stores/authStore';

vi.mock('../../../services/adminService');

// react-leaflet needs a real canvas; stub it to plain nodes so the pin's
// coordinates can be read (the LocationBubble.test.jsx pattern).
vi.mock('react-leaflet', () => ({
  MapContainer: ({ children, center }) => (
    <div data-testid="map" data-center={JSON.stringify(center)}>{children}</div>
  ),
  TileLayer: () => <div data-testid="tiles" />,
  Marker: ({ position }) => <div data-testid="marker" data-position={JSON.stringify(position)} />,
}));

// `t(key, 'English fallback', vars)` returns the fallback with its {{vars}}
// filled in (Delivery.test.js:32-38), so every assertion below also pins the
// inline English the seed must reproduce.
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback, vars) => (typeof fallback === 'string'
      ? fallback.replace(/{{\s*(\w+)\s*}}/g, (_, name) => String(vars?.[name]))
      : key),
  }),
}));

// antd's static message mounts a real portal outside act() and leaks between tests.
vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return {
    ...actual,
    message: {
      success: vi.fn(),
      error: vi.fn(),
      info: vi.fn(),
      warning: vi.fn(),
      loading: vi.fn(),
      destroy: vi.fn(),
      open: vi.fn(),
    },
  };
});

const CASE_ID = 9;
const HANDLED = [
  'SHARED_PLACE_NOT_VERIFIED',
  'SHARED_PLACE_RESOLVE_INVALID',
  'SHARED_PLACE_RESOLUTION_CONFLICT',
];
const ALL_KINDS = ['shared_place', 'same_home', 'other_place', 'own_bottles', 'wrong_count', 'close'];

const AKMAL = { id: 21, first_name: 'Akmal', last_name: 'Karimov', phone: '+998790000021' };
const AKMAL_LINKED = { id: 21, first_name: 'Akmal', last_name: 'Karimov' };

/** Akmal's ungrouped office, 15 m away with 8 empties: the place the driver picked. */
const akmalCandidate = (live = {}) => ({
  index: 0,
  ref: { kind: 'address', id: 40 },
  first_name: 'Akmal',
  empties: 8,
  distance_m: 15,
  live: {
    exists: true,
    place_group_id: null,
    label: null,
    empties: 8,
    members: [{ address_id: 40, title: 'Office', full_address: 'Chilonzor 5, 14', owner: AKMAL }],
    ...live,
  },
});

/** The same frozen candidate, now a member of the place group 60 "Office 12". */
const groupedCandidate = () => akmalCandidate({
  place_group_id: 60,
  label: 'Office 12',
  members: [
    { address_id: 40, title: 'Office', full_address: 'Chilonzor 5, 14', owner: AKMAL },
    {
      address_id: 41,
      title: 'Office',
      full_address: 'Chilonzor 5, 14',
      owner: { id: 22, first_name: 'Zarina', last_name: 'Usmonova', phone: '+998790000022' },
    },
  ],
});

const AKMAL_SOURCE = { address_id: 40, place_group_id: null, label: null, empties: 8, owner_first_name: 'Akmal' };

/**
 * The detail payload in the Shared-contracts shape (review.py case_detail). Every
 * number is distinct so a swapped field cannot pass: delivered 2, returned 3,
 * before 0, this address now -1, the candidate 8 empties at 15 m.
 */
const caseDetail = (overrides = {}) => ({
  id: CASE_ID,
  status: 'awaiting_admin',
  driver_status: 'answered',
  created_at: '2026-11-11T09:30:00+00:00',
  order: { id: 501, order_number: 'TG_000501_26' },
  delivery_id: 301,
  customer: { id: 11, first_name: 'Bobur', last_name: 'Yangi', phone: '+998790000011' },
  address: {
    id: 17,
    title: 'Home',
    full_address: 'Chilonzor 5, 12',
    latitude: 41.3101,
    longitude: 69.2801,
    apartment_number: '12',
    place_group: null,
    place_empties_now: -1,
  },
  driver: { id: 5, first_name: 'Dilshod' },
  delivered_qty: 2,
  returned_qty: 3,
  place_balance_before: 0,
  answer: { kind: 'picked', picked_index: 0, note: "Akmal's coworker", answered_at: '2026-11-11T09:31:00+00:00' },
  candidates: [akmalCandidate()],
  empty_reason: null,
  facts: { linked_accounts: [], referred_by: null },
  tryouts: [],
  move_sources: [],
  choices: { kinds: ALL_KINDS, move_allowed: true, grouping_done: false },
  claim: null,
  resolution: null,
  ...overrides,
});

/** The case address is already in the place group 60 "Office 12". */
const groupedCase = (overrides = {}) => caseDetail({
  address: { ...caseDetail().address, place_group: { id: 60, label: 'Office 12' } },
  choices: { kinds: ALL_KINDS, move_allowed: false, grouping_done: true },
  ...overrides,
});

const MERGE_PREVIEW = {
  entries: [
    {
      id: 5,
      quantity: 8,
      event_type: 'admin_adjustment',
      occurred_at: '2026-10-01T10:00:00+00:00',
      user_name: 'Akmal Karimov',
      preview_balance_after: 8,
      excluded: false,
    },
    {
      id: 6,
      quantity: -3,
      event_type: 'delivery',
      occurred_at: '2026-11-11T09:30:00+00:00',
      user_name: 'Bobur Yangi',
      preview_balance_after: 5,
      excluded: false,
    },
  ],
  entry_ids: [5, 6],
  computed_balance: 5,
  stored_balance: 5,
  drift: 0,
  excluded_total: 0,
  resulting_balance: 5,
  projected_place_balance: 5,
};

const renderCase = (detail, { after } = {}) => {
  if (after) {
    adminService.getSharedPlaceCase
      .mockResolvedValueOnce({ case: detail })
      .mockResolvedValue({ case: after });
  } else {
    adminService.getSharedPlaceCase.mockResolvedValue({ case: detail });
  }
  const onViewUser = vi.fn();
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <SharedPlaceCaseDetail caseId={CASE_ID} onViewUser={onViewUser} />
      </QueryClientProvider>
    </MemoryRouter>
  );
  return { onViewUser };
};

/** The (last) open modal whose title reads exactly `title`. */
const modalTitled = (title) => waitFor(() => {
  const titles = [...document.querySelectorAll('.ant-modal-title')].filter((el) => el.textContent === title);
  expect(titles.length).toBeGreaterThan(0);
  return titles[titles.length - 1].closest('.ant-modal');
});

const noModalTitled = (title) =>
  [...document.querySelectorAll('.ant-modal-title')].every((el) => el.textContent !== title);

const choice = (name) => screen.findByRole('button', { name });

// Strict: a field the choice omits must be absent, not `undefined`.
const resolvedWith = async (payload) => {
  await waitFor(() => expect(adminService.resolveSharedPlaceCase).toHaveBeenCalledTimes(1));
  expect(adminService.resolveSharedPlaceCase.mock.calls[0]).toStrictEqual([
    CASE_ID,
    payload,
    { handledErrorCodes: HANDLED },
  ]);
};

const confirmIn = async (user, modal, reason) => {
  if (reason !== undefined) {
    await user.type(within(modal).getByPlaceholderText('Reason (required)'), reason);
  }
  await user.click(within(modal).getByRole('button', { name: 'Confirm' }));
};

/** The grouping dialog (PlaceGroupConfirmModal): type the label and reason, then OK. */
const confirmGrouping = async (user, modal, { label, reason }) => {
  if (label !== undefined) {
    await user.type(within(modal).getByPlaceholderText('Label (e.g. Acme office)'), label);
  }
  if (reason !== undefined) {
    await user.type(within(modal).getByPlaceholderText('Reason (required)'), reason);
  }
  await user.click(within(modal).getByRole('button', { name: 'OK' }));
};

/** Drive the real merge review: exclude entry 5, then confirm the review. */
const excludeEntryFive = async (user, groupingModal) => {
  await user.click(within(groupingModal).getByRole('button', { name: 'Review bottle history' }));
  const review = await modalTitled('Review the merged bottle history');
  const row = within(review).getByText(/admin_adjustment/).closest('li');
  await user.click(within(row).getByRole('checkbox'));
  await waitFor(() => expect(adminService.getPlaceGroupMergePreview).toHaveBeenCalledTimes(2));
  await user.click(within(review).getByRole('button', { name: 'OK' }));
};

const GROUP_60 = (notes) => ({
  data: {
    place_group_id: 60,
    label: 'Office 12',
    notes,
    place_balance: 8,
    members: [],
    removal_dissolves_group: false,
    events: [],
  },
});

describe('SharedPlaceCaseDetail', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({ permissions: { can_review_shared_places: true, can_manage_users: true } });
    adminService.resolveSharedPlaceCase.mockResolvedValue({ case: caseDetail({ status: 'resolved' }) });
    adminService.createPlaceGroup.mockResolvedValue({ data: { place_group_id: 70 } });
    adminService.addPlaceGroupAddresses.mockResolvedValue({ data: { place_group_id: 60 } });
    adminService.renamePlaceGroup.mockResolvedValue({ data: { place_group_id: 60 } });
    adminService.linkAccounts.mockResolvedValue({ data: { canonical_customer_id: 11 } });
    adminService.getPlaceGroupMergePreview.mockResolvedValue({ data: MERGE_PREVIEW });
  });

  afterEach(() => useAuthStore.setState({ permissions: {} }));

  // ---------------------------------------------------------------- reading

  it('shows what happened from the payload, and the customer opens the user', async () => {
    const user = userEvent.setup();
    const { onViewUser } = renderCase(caseDetail());

    expect(await screen.findByText('TG_000501_26')).toBeInTheDocument();
    expect(
      screen.getByText('got 2 full, handed back 3 empties; this address had 0 on record before')
    ).toBeInTheDocument();
    expect(screen.getByText(/by Dilshod$/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Bobur Yangi' }));
    expect(onViewUser.mock.calls).toEqual([[11]]);
  });

  it('shows the driver\'s pick with the frozen figures, then the note, and the read-only pin', async () => {
    renderCase(caseDetail());

    expect(await screen.findByText('Picked: Akmal · 8 empties · 15 m')).toBeInTheDocument();
    expect(screen.getByText("Akmal's coworker")).toBeInTheDocument();
    expect(screen.getByTestId('marker')).toHaveAttribute('data-position', JSON.stringify([41.3101, 69.2801]));
  });

  it.each([
    ['answered', { kind: 'none', picked_index: null, note: null, answered_at: '2026-11-11T09:31:00+00:00' }, 'None of these'],
    ['answered', { kind: 'skipped', picked_index: null, note: null, answered_at: '2026-11-11T09:31:00+00:00' }, 'Skipped'],
    ['no_answer', null, "Didn't answer"],
    ['not_asked', null, 'Entered by an admin'],
  ])('a %s case says what the driver did', async (driverStatus, answer, text) => {
    renderCase(caseDetail({ driver_status: driverStatus, answer, driver: driverStatus === 'not_asked' ? null : { id: 5, first_name: 'Dilshod' } }));
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.queryByText(/^Picked:/)).toBeNull();
  });

  it('a note typed on the no-candidates screen reads "No nearby places" plus the note, never "None of these"', async () => {
    renderCase(caseDetail({
      candidates: [],
      empty_reason: 'none_within_radius',
      answer: {
        kind: 'no_candidates',
        picked_index: null,
        note: "says he ordered before on his wife's phone",
        answered_at: '2026-11-11T09:31:00+00:00',
      },
    }));

    expect(await screen.findByText('No nearby places')).toBeInTheDocument();
    expect(screen.getByText("says he ordered before on his wife's phone")).toBeInTheDocument();
    expect(screen.getByText('No other customer keeps empties close to this address.')).toBeInTheDocument();
    expect(screen.queryByText('None of these')).toBeNull();
  });

  it('a note without an answer renders the note and no picked/none/skipped line', async () => {
    renderCase(caseDetail({
      driver_status: 'no_answer',
      answer: { kind: null, picked_index: null, note: 'Gate code 12', answered_at: null },
    }));

    expect(await screen.findByText('Gate code 12')).toBeInTheDocument();
    expect(screen.getByText("Didn't answer")).toBeInTheDocument();
    expect(screen.queryByText(/^Picked:/)).toBeNull();
    expect(screen.queryByText('None of these')).toBeNull();
    expect(screen.queryByText('Skipped')).toBeNull();
  });

  it('a frozen candidate that is gone still renders, as "no longer exists"', async () => {
    renderCase(caseDetail({
      candidates: [akmalCandidate({ exists: false, place_group_id: null, label: null, empties: 0, members: [] })],
    }));
    expect(await screen.findByText('No longer exists')).toBeInTheDocument();
  });

  it('with no candidates, the empty_reason is explained', async () => {
    renderCase(caseDetail({ candidates: [], empty_reason: 'no_pin', answer: null, driver_status: 'no_answer' }));
    expect(
      await screen.findByText('This address has no map pin, so no nearby places were looked up.')
    ).toBeInTheDocument();
  });

  it('shows the live facts only when they are true', async () => {
    renderCase(groupedCase({
      facts: {
        linked_accounts: [AKMAL_LINKED, { id: 24, first_name: 'Sardor', last_name: 'Aliyev' }],
        referred_by: { id: 30, first_name: 'Nodira', last_name: 'Rahimova' },
      },
    }));

    expect(await screen.findByText('Already linked to Akmal Karimov, Sardor Aliyev')).toBeInTheDocument();
    expect(screen.getByText('Referred by Nodira Rahimova')).toBeInTheDocument();
    expect(screen.getByText("This address has since been grouped into 'Office 12'")).toBeInTheDocument();
  });

  it('hides the live facts that are not true', async () => {
    renderCase(caseDetail());
    await screen.findByText('TG_000501_26');
    expect(screen.queryByText(/Already linked to/)).toBeNull();
    expect(screen.queryByText(/Referred by/)).toBeNull();
    expect(screen.queryByText(/has since been grouped/)).toBeNull();
  });

  it('a try-out links to its page and the hint books +returned_qty', async () => {
    renderCase(caseDetail({
      tryouts: [{ tryout_id: 7, tryout_number: 'TRY_000007_26', outstanding_bottles_total: 2 }],
    }));

    const link = await screen.findByRole('link', { name: 'TRY_000007_26' });
    expect(link).toHaveAttribute('href', '/tryouts?tryout_id=7');
    expect(screen.getByText('2 trial bottles still out')).toBeInTheDocument();
    expect(
      screen.getByText(
        'Settle the trial bottles on the try-out, then choose "Customer\'s own bottles" (+3) so this address isn\'t short.'
      )
    ).toBeInTheDocument();
  });

  it('a settled try-out (0 out) keeps its link and the hint, without a "0 still out" line', async () => {
    renderCase(caseDetail({
      tryouts: [{ tryout_id: 7, tryout_number: 'TRY_000007_26', outstanding_bottles_total: 0 }],
    }));

    expect(await screen.findByRole('link', { name: 'TRY_000007_26' })).toHaveAttribute('href', '/tryouts?tryout_id=7');
    expect(screen.queryByText(/trial bottles still out/)).toBeNull();
    expect(
      screen.getByText(
        'Settle the trial bottles on the try-out, then choose "Customer\'s own bottles" (+3) so this address isn\'t short.'
      )
    ).toBeInTheDocument();
  });

  // ---------------------------------------------------------------- choices

  it('draws exactly the buttons in choices.kinds', async () => {
    renderCase(caseDetail({
      choices: { kinds: ['own_bottles'], move_allowed: true, grouping_done: false },
      claim: { kind: 'own_bottles', payload: { kind: 'own_bottles' } },
    }));

    expect(await choice("Customer's own bottles")).toBeInTheDocument();
    for (const name of [
      'Shared place — different people, one place',
      'Same person, same home',
      'Same person, a different place',
      'Wrong count',
      'Close, no action',
    ]) {
      expect(screen.queryByRole('button', { name })).toBeNull();
    }
  });

  it('a settled case shows its outcome and offers no choice', async () => {
    renderCase(caseDetail({
      status: 'closed',
      choices: { kinds: [], move_allowed: false, grouping_done: false },
      resolution: {
        kind: 'close',
        payload: { kind: 'close' },
        reason: 'Handled on the phone',
        decided_by: { id: 1, first_name: 'Admin' },
        decided_at: '2026-11-11T10:00:00+00:00',
        resulting_group_id: null,
      },
    }));

    expect(await screen.findByText('Handled on the phone')).toBeInTheDocument();
    expect(screen.getByText('Close, no action')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Close, no action' })).toBeNull();
    expect(screen.queryByRole('button', { name: "Customer's own bottles" })).toBeNull();
  });

  it('D sends exactly own_bottles with the reason', async () => {
    const user = userEvent.setup();
    renderCase(caseDetail());

    await user.click(await choice("Customer's own bottles"));
    const modal = await modalTitled("Customer's own bottles");
    expect(within(modal).getByText('+3 at this address')).toBeInTheDocument();
    await confirmIn(user, modal, '  Customer brought own bottles  ');

    await resolvedWith({ kind: 'own_bottles', reason: 'Customer brought own bottles' });
  });

  it('D on a grouped address books at the place', async () => {
    const user = userEvent.setup();
    renderCase(groupedCase());

    await user.click(await choice("Customer's own bottles"));
    const modal = await modalTitled("Customer's own bottles");
    expect(within(modal).getByText("+3 at the place 'Office 12'")).toBeInTheDocument();
  });

  it('E sends wrong_count with the typed number, 0 included', async () => {
    const user = userEvent.setup();
    renderCase(caseDetail());

    await user.click(await choice('Wrong count'));
    const modal = await modalTitled('Wrong count');
    expect(
      within(modal).getByText('The driver typed 3 empties handed back. How many were actually handed back?')
    ).toBeInTheDocument();
    expect(within(modal).getByText('The difference is booked at this address.')).toBeInTheDocument();
    await user.type(within(modal).getByRole('spinbutton'), '0');
    await confirmIn(user, modal, 'Driver mistyped');

    await resolvedWith({ kind: 'wrong_count', actual_returned: 0, reason: 'Driver mistyped' });
  });

  it('E on a grouped address says the place', async () => {
    const user = userEvent.setup();
    renderCase(groupedCase());

    await user.click(await choice('Wrong count'));
    const modal = await modalTitled('Wrong count');
    expect(
      within(modal).getByText('The driver typed 3 empties handed back. How many were actually handed back?')
    ).toBeInTheDocument();
    expect(within(modal).getByText("The difference is booked at the place 'Office 12'.")).toBeInTheDocument();
  });

  it.each([
    ['-1', 'Enter 0 or more'],
    ['3', 'That is the number the driver typed. Enter the number actually handed back.'],
  ])('E refuses %s in the form, keeps the dialog open and sends nothing', async (typed, error) => {
    const user = userEvent.setup();
    renderCase(caseDetail());

    await user.click(await choice('Wrong count'));
    const modal = await modalTitled('Wrong count');
    const count = within(modal).getByRole('spinbutton');
    await user.type(count, typed);
    await confirmIn(user, modal, 'Driver mistyped');

    expect(await within(modal).findByText(error)).toBeInTheDocument();
    // Never clamped to 0: the typed number stays what the admin typed.
    expect(count).toHaveValue(typed);
    expect(adminService.resolveSharedPlaceCase).not.toHaveBeenCalled();
  });

  it('F closes the case with the reason', async () => {
    const user = userEvent.setup();
    renderCase(caseDetail());

    await user.click(await choice('Close, no action'));
    const modal = await modalTitled('Close, no action');
    await confirmIn(user, modal, 'Handled elsewhere');

    await resolvedWith({ kind: 'close', reason: 'Handled elsewhere' });
  });

  it('a reason is required: an empty one sends nothing', async () => {
    const user = userEvent.setup();
    renderCase(caseDetail());

    await user.click(await choice('Close, no action'));
    const modal = await modalTitled('Close, no action');
    await confirmIn(user, modal, '   ');

    expect(await within(modal).findByText('Enter a reason')).toBeInTheDocument();
    expect(adminService.resolveSharedPlaceCase).not.toHaveBeenCalled();
  });

  it('C after the link refetches the case, then moves from the picked place', async () => {
    const user = userEvent.setup();
    renderCase(caseDetail(), {
      after: caseDetail({
        facts: { linked_accounts: [AKMAL_LINKED], referred_by: null },
        move_sources: [AKMAL_SOURCE],
      }),
    });

    await user.click(await choice('Same person, a different place'));
    const link = await modalTitled('Link this account?');
    await user.type(within(link).getByPlaceholderText('Enter reason'), 'same man, second flat');
    await user.click(within(link).getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(adminService.linkAccounts.mock.calls).toEqual([[11, 21, 'same man, second flat']]));

    // The move facts exist only in the refetched case: the first one had no move_sources.
    const modal = await modalTitled('Same person, a different place');
    expect(
      within(modal).getByText('handed back 3 today; this address now shows -1; Akmal shows 8')
    ).toBeInTheDocument();
    // Pre-selected because the driver picked Akmal's office; the count starts empty.
    expect(within(modal).getByTitle('Akmal · 8 empties')).toBeInTheDocument();
    const count = within(modal).getByRole('spinbutton');
    expect(count).toHaveValue('');
    await user.type(count, '3');
    // The reason typed in the link step is pre-filled.
    expect(within(modal).getByPlaceholderText('Reason (required)')).toHaveValue('same man, second flat');
    await confirmIn(user, modal);

    await resolvedWith({
      kind: 'other_place',
      target_user_id: 21,
      move_count: 3,
      source_address_id: 40,
      reason: 'same man, second flat',
    });
  });

  it('C with an empty count links only, and skips the link step for a linked account', async () => {
    const user = userEvent.setup();
    renderCase(caseDetail({
      facts: { linked_accounts: [AKMAL_LINKED], referred_by: null },
      move_sources: [AKMAL_SOURCE],
    }));

    await user.click(await choice('Same person, a different place'));
    const modal = await modalTitled('Same person, a different place');
    await confirmIn(user, modal, 'second home');

    await resolvedWith({ kind: 'other_place', target_user_id: 21, reason: 'second home' });
    expect(adminService.linkAccounts).not.toHaveBeenCalled();
    expect(noModalTitled('Link this account?')).toBe(true);
  });

  it('C refuses a count above the source\'s empties', async () => {
    const user = userEvent.setup();
    renderCase(caseDetail({
      facts: { linked_accounts: [AKMAL_LINKED], referred_by: null },
      move_sources: [AKMAL_SOURCE],
    }));

    await user.click(await choice('Same person, a different place'));
    const modal = await modalTitled('Same person, a different place');
    await user.type(within(modal).getByRole('spinbutton'), '9');
    await confirmIn(user, modal, 'second home');

    expect(await within(modal).findByText('Enter a number from 1 to 8')).toBeInTheDocument();
    expect(adminService.resolveSharedPlaceCase).not.toHaveBeenCalled();
  });

  it('the move field is hidden when move_allowed is false', async () => {
    const user = userEvent.setup();
    renderCase(groupedCase({
      facts: { linked_accounts: [AKMAL_LINKED], referred_by: null },
      move_sources: [AKMAL_SOURCE],
    }));

    await user.click(await choice('Same person, a different place'));
    const modal = await modalTitled('Same person, a different place');
    expect(within(modal).queryByRole('spinbutton')).toBeNull();
    expect(
      within(modal).getByText('This address is already in a place group, so no empties can move here.')
    ).toBeInTheDocument();
    await confirmIn(user, modal, 'second home');

    await resolvedWith({ kind: 'other_place', target_user_id: 21, reason: 'second home' });
  });

  it('a stored claim pre-fills the confirmation, so a retry repeats it exactly', async () => {
    const user = userEvent.setup();
    renderCase(caseDetail({
      facts: { linked_accounts: [AKMAL_LINKED], referred_by: null },
      move_sources: [{ ...AKMAL_SOURCE, empties: 5 }],
      choices: { kinds: ['other_place'], move_allowed: true, grouping_done: false },
      claim: {
        kind: 'other_place',
        payload: { kind: 'other_place', target_user_id: 21, move_count: 3, source_address_id: 40 },
      },
    }));

    await user.click(await choice('Same person, a different place'));
    const modal = await modalTitled('Same person, a different place');
    expect(within(modal).getByRole('spinbutton')).toHaveValue('3');
    await confirmIn(user, modal, 'retry after a timeout');

    await resolvedWith({
      kind: 'other_place',
      target_user_id: 21,
      move_count: 3,
      source_address_id: 40,
      reason: 'retry after a timeout',
    });
  });

  it('A creates the place from the case address and the candidate, then resolves', async () => {
    const user = userEvent.setup();
    renderCase(caseDetail());

    await user.click(await choice('Shared place — different people, one place'));
    const modal = await modalTitled('Group these addresses as one place?');
    // The label starts empty; the note is the driver's.
    expect(within(modal).getByPlaceholderText('Label (e.g. Acme office)')).toHaveValue('');
    expect(within(modal).getByPlaceholderText('Place note (optional)')).toHaveValue("Akmal's coworker");
    await confirmGrouping(user, modal, { label: 'Office 12', reason: 'coworkers' });

    await waitFor(() => expect(adminService.createPlaceGroup).toHaveBeenCalledTimes(1));
    expect(adminService.createPlaceGroup.mock.calls[0]).toEqual([
      [17, 40],
      'Office 12',
      'coworkers',
      {},
      { notes: "Akmal's coworker" },
    ]);
    await resolvedWith({ kind: 'shared_place', target_address_id: 40, reason: 'coworkers' });
  });

  it('A forwards the merge review to createPlaceGroup', async () => {
    const user = userEvent.setup();
    renderCase(caseDetail());

    await user.click(await choice('Shared place — different people, one place'));
    const modal = await modalTitled('Group these addresses as one place?');
    await excludeEntryFive(user, modal);
    await confirmGrouping(user, modal, { reason: 'coworkers' });

    await waitFor(() => expect(adminService.createPlaceGroup).toHaveBeenCalledTimes(1));
    expect(adminService.createPlaceGroup.mock.calls[0][3]).toEqual({
      excludedLedgerEntryIds: [5],
      previewEntryIds: [5, 6],
    });
  });

  it('A onto a group forwards the merge review to addPlaceGroupAddresses and omits the target', async () => {
    const user = userEvent.setup();
    adminService.getPlaceGroup.mockResolvedValue(GROUP_60('Gate 12'));
    renderCase(caseDetail({ candidates: [groupedCandidate()] }));

    await user.click(await choice('Shared place — different people, one place'));
    const modal = await modalTitled('Add an address to this place group?');
    await excludeEntryFive(user, modal);
    await confirmGrouping(user, modal, { reason: 'joins the office' });

    await waitFor(() => expect(adminService.addPlaceGroupAddresses).toHaveBeenCalledTimes(1));
    expect(adminService.getPlaceGroup.mock.calls).toEqual([[60]]);
    expect(adminService.addPlaceGroupAddresses.mock.calls[0]).toEqual([
      60,
      [17],
      'joins the office',
      { excludedLedgerEntryIds: [5], previewEntryIds: [5, 6] },
    ]);
    await resolvedWith({ kind: 'shared_place', reason: 'joins the office' });
  });

  it('A onto a group keeps its note: no rename when neither the label nor the note changed', async () => {
    const user = userEvent.setup();
    adminService.getPlaceGroup.mockResolvedValue(GROUP_60('Gate 12'));
    renderCase(caseDetail({ candidates: [groupedCandidate()] }));

    await user.click(await choice('Shared place — different people, one place'));
    const modal = await modalTitled('Add an address to this place group?');
    expect(within(modal).getByPlaceholderText('Label (e.g. Acme office)')).toHaveValue('Office 12');
    expect(within(modal).getByPlaceholderText('Place note (optional)')).toHaveValue('Gate 12');
    await confirmGrouping(user, modal, { reason: 'joins the office' });

    await resolvedWith({ kind: 'shared_place', reason: 'joins the office' });
    expect(adminService.addPlaceGroupAddresses.mock.calls[0]).toEqual([60, [17], 'joins the office', {}]);
    expect(adminService.renamePlaceGroup).not.toHaveBeenCalled();
  });

  it('A onto a group with no note saves the driver\'s note, keeping the label', async () => {
    const user = userEvent.setup();
    adminService.getPlaceGroup.mockResolvedValue(GROUP_60(null));
    renderCase(caseDetail({ candidates: [groupedCandidate()] }));

    await user.click(await choice('Shared place — different people, one place'));
    const modal = await modalTitled('Add an address to this place group?');
    expect(within(modal).getByPlaceholderText('Place note (optional)')).toHaveValue("Akmal's coworker");
    await confirmGrouping(user, modal, { reason: 'joins the office' });

    await resolvedWith({ kind: 'shared_place', reason: 'joins the office' });
    expect(adminService.renamePlaceGroup.mock.calls).toEqual([
      [60, null, 'joins the office', { notes: "Akmal's coworker" }],
    ]);
  });

  it('A onto a group renames it when the label changed', async () => {
    const user = userEvent.setup();
    adminService.getPlaceGroup.mockResolvedValue(GROUP_60('Gate 12'));
    renderCase(caseDetail({ candidates: [groupedCandidate()] }));

    await user.click(await choice('Shared place — different people, one place'));
    const modal = await modalTitled('Add an address to this place group?');
    const label = within(modal).getByPlaceholderText('Label (e.g. Acme office)');
    await user.clear(label);
    await confirmGrouping(user, modal, { label: 'Tower B', reason: 'renamed' });

    await resolvedWith({ kind: 'shared_place', reason: 'renamed' });
    expect(adminService.renamePlaceGroup.mock.calls).toEqual([[60, 'Tower B', 'renamed', { notes: 'Gate 12' }]]);
  });

  it('A onto a group: a rename that failed after the add is retried alone, then the case resolves', async () => {
    const user = userEvent.setup();
    adminService.getPlaceGroup.mockResolvedValue(GROUP_60('Gate 12'));
    adminService.renamePlaceGroup.mockRejectedValueOnce({
      response: {
        status: 409,
        data: { success: false, message: 'Busy', error_code: 'PLACE_GROUP_BUSY' },
      },
    });
    renderCase(caseDetail({ candidates: [groupedCandidate()] }));

    await user.click(await choice('Shared place — different people, one place'));
    const modal = await modalTitled('Add an address to this place group?');
    const label = within(modal).getByPlaceholderText('Label (e.g. Acme office)');
    await user.clear(label);
    await confirmGrouping(user, modal, { label: 'Tower B', reason: 'renamed' });
    await waitFor(() => expect(adminService.renamePlaceGroup).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(message.error).toHaveBeenCalledTimes(1));
    expect(adminService.resolveSharedPlaceCase).not.toHaveBeenCalled();

    // The same dialog, OK again: the address is already in group 60, so only the rename runs.
    await user.click(within(modal).getByRole('button', { name: 'OK' }));

    await resolvedWith({ kind: 'shared_place', reason: 'renamed' });
    expect(adminService.addPlaceGroupAddresses.mock.calls).toEqual([[60, [17], 'renamed', {}]]);
    expect(adminService.renamePlaceGroup.mock.calls).toEqual([
      [60, 'Tower B', 'renamed', { notes: 'Gate 12' }],
      [60, 'Tower B', 'renamed', { notes: 'Gate 12' }],
    ]);
  });

  it('a group-fence refusal is explained and nothing is resolved', async () => {
    const user = userEvent.setup();
    adminService.createPlaceGroup.mockRejectedValue({
      response: {
        status: 400,
        data: { success: false, message: 'Validation failed', error_code: 'PLACE_GROUP_ADDRESS_ALREADY_GROUPED' },
      },
    });
    renderCase(caseDetail());

    await user.click(await choice('Shared place — different people, one place'));
    const modal = await modalTitled('Group these addresses as one place?');
    await confirmGrouping(user, modal, { reason: 'coworkers' });

    // The fence code's own copy (placeGroupCopy.js), never the envelope's generic "Validation failed".
    await waitFor(() => expect(message.error.mock.calls).toEqual([
      ['That address is already in another place group. Remove it from that group first.'],
    ]));
    expect(adminService.resolveSharedPlaceCase).not.toHaveBeenCalled();
  });

  it('with grouping_done, "Confirm: shared place" resolves without any grouping dialog', async () => {
    const user = userEvent.setup();
    renderCase(groupedCase());

    await user.click(await choice("Confirm: shared place 'Office 12'"));
    const modal = await modalTitled("Confirm: shared place 'Office 12'");
    await confirmIn(user, modal, 'grouped yesterday');

    await resolvedWith({ kind: 'shared_place', reason: 'grouped yesterday' });
    expect(adminService.getPlaceGroup).not.toHaveBeenCalled();
    expect(adminService.createPlaceGroup).not.toHaveBeenCalled();
    expect(adminService.addPlaceGroupAddresses).not.toHaveBeenCalled();
    expect(noModalTitled('Group these addresses as one place?')).toBe(true);
    expect(noModalTitled('Add an address to this place group?')).toBe(true);
  });

  it('with grouping_done, "Confirm: same person" links first, then resolves same_home', async () => {
    const user = userEvent.setup();
    renderCase(groupedCase());

    await user.click(await choice('Confirm: same person, same home'));
    const link = await modalTitled('Link this account?');
    await user.type(within(link).getByPlaceholderText('Enter reason'), 'brothers');
    await user.click(within(link).getByRole('button', { name: 'Confirm' }));

    const modal = await modalTitled('Confirm: same person, same home');
    expect(within(modal).getByPlaceholderText('Reason (required)')).toHaveValue('brothers');
    await confirmIn(user, modal);

    await resolvedWith({ kind: 'same_home', target_user_id: 21, reason: 'brothers' });
    expect(adminService.createPlaceGroup).not.toHaveBeenCalled();
  });

  it('B with the target already linked skips LinkAccountsConfirm and goes straight to grouping', async () => {
    const user = userEvent.setup();
    renderCase(caseDetail({ facts: { linked_accounts: [AKMAL_LINKED], referred_by: null } }));

    await user.click(await choice('Same person, same home'));
    const modal = await modalTitled('Group these addresses as one place?');
    expect(noModalTitled('Link this account?')).toBe(true);
    await confirmGrouping(user, modal, { reason: 'one family' });

    await waitFor(() => expect(adminService.createPlaceGroup).toHaveBeenCalledTimes(1));
    expect(adminService.createPlaceGroup.mock.calls[0]).toEqual([
      [17, 40],
      null,
      'one family',
      {},
      { notes: "Akmal's coworker" },
    ]);
    await resolvedWith({ kind: 'same_home', target_user_id: 21, target_address_id: 40, reason: 'one family' });
    expect(adminService.linkAccounts).not.toHaveBeenCalled();
  });

  it('B links first and pre-fills the link reason into the grouping dialog', async () => {
    const user = userEvent.setup();
    renderCase(caseDetail());

    await user.click(await choice('Same person, same home'));
    const link = await modalTitled('Link this account?');
    await user.type(within(link).getByPlaceholderText('Enter reason'), 'same man');
    await user.click(within(link).getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(adminService.linkAccounts.mock.calls).toEqual([[11, 21, 'same man']]));

    const modal = await modalTitled('Group these addresses as one place?');
    expect(within(modal).getByPlaceholderText('Reason (required)')).toHaveValue('same man');
    await confirmGrouping(user, modal, {});

    await resolvedWith({ kind: 'same_home', target_user_id: 21, target_address_id: 40, reason: 'same man' });
  });

  it('a search hit in a group shows as its place and becomes the A target', async () => {
    const user = userEvent.setup();
    adminService.searchAddresses.mockResolvedValue({
      data: {
        addresses: [{
          address_id: 55,
          title: 'Tower',
          full_address: 'Yunusobod 2',
          address_group_id: 61,
          address_group_label: 'Tower B',
          owner: { id: 23, first_name: 'Laylo', last_name: 'Saidova', phone: '+998790000023' },
        }],
      },
    });
    adminService.getPlaceGroup.mockResolvedValue({
      data: { ...GROUP_60(null).data, place_group_id: 61, label: 'Tower B' },
    });
    renderCase(caseDetail());

    await user.type(await screen.findByPlaceholderText('Find an address by phone, name or address'), 'Laylo{Enter}');
    await waitFor(() => expect(adminService.searchAddresses.mock.calls).toEqual([['Laylo', false]]));
    await user.click(await screen.findByRole('radio', { name: /The place 'Tower B'/ }));

    await user.click(screen.getByRole('button', { name: 'Shared place — different people, one place' }));
    await modalTitled('Add an address to this place group?');
    expect(adminService.getPlaceGroup.mock.calls).toEqual([[61]]);
  });

  it('a SHARED_PLACE_NOT_VERIFIED refusal is shown inline', async () => {
    const user = userEvent.setup();
    adminService.resolveSharedPlaceCase.mockRejectedValue({
      response: {
        status: 409,
        data: {
          success: false,
          message: 'The address is not in one place group with the claimed place',
          error_code: 'SHARED_PLACE_NOT_VERIFIED',
        },
      },
    });
    renderCase(groupedCase());

    await user.click(await choice("Confirm: shared place 'Office 12'"));
    const modal = await modalTitled("Confirm: shared place 'Office 12'");
    await confirmIn(user, modal, 'grouped yesterday');

    const refusal = await screen.findByText(
      'The grouping or link this choice needs is not in place now. Nothing was recorded.'
    );
    // Inline on the case screen, not in a closed dialog; the case is read again.
    expect(refusal.closest('.ant-alert')).not.toBeNull();
    expect(noModalTitled("Confirm: shared place 'Office 12'")).toBe(true);
    await waitFor(() => expect(adminService.getSharedPlaceCase.mock.calls.length).toBeGreaterThan(1));
  });
});
